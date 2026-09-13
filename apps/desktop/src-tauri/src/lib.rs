//! NovaBrief for Windows.
//!
//! The application has no main window. It lives in the notification area and
//! opens a window only when somebody asks for one (EF-12), because the product
//! it competes with is "remember to start the recorder", and an app that wants
//! a window on screen loses that contest before the meeting begins.

pub mod devices;
pub mod engine;
pub mod paths;
pub mod recording;
pub mod session;
pub mod state;

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use audio_engine::encode::SegmentedOpusWriter;
use audio_engine::pipeline::Endpoints;
use engine::{Engine, EngineError, Outcome, Snapshot, Source};
use recording::VaultSegmentSink;
use session::Session;
use state::{tray_menu, AppState, MenuItem};
use tauri::Manager;
use vault::{AccountKey, DeviceSecret, DpapiSealer, Vault};

/// Bits per second handed to Opus.
///
/// 32 kbit/s stereo at 16 kHz: an hour of meeting is about 14 MB, which is the
/// difference between an upload that finishes over a Douala connection and one
/// that does not. **Not a plan value and not a price** (ADR-09) - it is the
/// codec setting the transcription providers were measured against.
const BITRATE: u32 = 32_000;

/// Seconds of audio per encrypted segment.
///
/// The lower end of the 5-10 s window the POC fixed: a crash costs at most one
/// segment, so the shorter it is the less EF-17 has to promise away.
const SEGMENT_SECONDS: f64 = 5.0;

/// What the application is doing, shared between the tray and any open window.
///
/// Holds no session of its own. The rules live on the recording thread with
/// the audio they apply to, which is what stops the tray and the widget from
/// each keeping a copy of "how long have we been recording" and disagreeing.
#[derive(Debug)]
pub struct Recorder {
    /// Where recordings are written; `None` if the machine has no usable
    /// `%LOCALAPPDATA%`, which is reported when somebody tries to record
    /// rather than by refusing to start.
    root: Option<PathBuf>,
    account: Option<AccountKey>,
    engine: Mutex<Option<Engine>>,
    /// The last finished recording, waiting for the uploader of lot L3.6.
    completed: Mutex<Option<Outcome>>,
    /// Why the last recording could not be closed, if it could not.
    ///
    /// Kept rather than dropped: a recording that ends on its own - the budget
    /// of EF-34, or both devices dying - has nobody waiting on a return value,
    /// so without this the failure would have no way to reach a person.
    fault: Mutex<Option<String>>,
}

impl Default for Recorder {
    fn default() -> Self {
        Self::new()
    }
}

impl Recorder {
    /// Open the local store.
    ///
    /// A machine whose store cannot be opened still gets an application: the
    /// failure is reported when somebody presses Record, where it can be read,
    /// rather than as a window that never appears.
    #[must_use]
    pub fn new() -> Self {
        let prepared = paths::data_root().and_then(|data_root| {
            let secret =
                DeviceSecret::load_or_create(&paths::device_key_path(&data_root), &DpapiSealer)
                    .map_err(|error| std::io::Error::other(error.to_string()))?;
            Ok((paths::recordings_root(&data_root), secret.account_key()))
        });

        let (root, account) = match prepared {
            Ok((root, account)) => (Some(root), Some(account)),
            Err(_) => (None, None),
        };

        Self::with_store(root, account)
    }

    /// A recorder that writes to `root`.
    ///
    /// The seam the tests record through, and what a configurable store
    /// location will use when somebody asks for one. Either argument being
    /// `None` means this machine cannot record; it is reported when somebody
    /// presses Record.
    #[must_use]
    pub fn with_store(root: Option<PathBuf>, account: Option<AccountKey>) -> Self {
        Self {
            root,
            account,
            engine: Mutex::new(None),
            completed: Mutex::new(None),
            fault: Mutex::new(None),
        }
    }

    /// The state, for the tray and the window.
    ///
    /// # Panics
    ///
    /// If a previous holder of the lock panicked. Nothing here can panic while
    /// holding it, so a poisoned lock means the process is already unsound and
    /// carrying on would hide that.
    #[must_use]
    pub fn state(&self) -> AppState {
        self.snapshot()
            .map_or(AppState::Idle, |snapshot| snapshot.state)
    }

    /// Everything the user interface draws, or `None` when nothing is going on.
    ///
    /// A finished recording keeps answering until the uploader has taken it:
    /// between the last segment and the first byte sent, the honest state is
    /// "uploading", not "idle".
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    #[must_use]
    pub fn snapshot(&self) -> Option<Snapshot> {
        self.reap();
        if let Some(engine) = self
            .engine
            .lock()
            .expect("the recorder lock is poisoned")
            .as_ref()
        {
            return Some(engine.snapshot());
        }
        let completed = self
            .completed
            .lock()
            .expect("the recorder lock is poisoned");
        completed.as_ref().map(|outcome| Snapshot {
            state: AppState::Uploading,
            meeting_id: outcome.meeting_id.clone(),
            debug_id: outcome.debug_id.clone(),
            recorded_ms: outcome.recorded.as_millis() as u64,
            discarded_ms: outcome.discarded.as_millis() as u64,
            microphone_level: 0.0,
            system_level: 0.0,
            warned: false,
            stalled: Vec::new(),
            failure: outcome.failure.clone(),
        })
    }

    /// Begin a recording that may keep `limit` of audio.
    ///
    /// The limit comes from the caller - lot L5 reads it from the plan - and
    /// never from a constant here (ADR-09). The session clamps it to the
    /// technical ceiling of EF-34.
    ///
    /// # Errors
    ///
    /// When a recording is already under way, when the local store cannot be
    /// opened, or when no audio device can be.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn start(
        &self,
        limit: Duration,
        meeting_id: &str,
        debug_id: &str,
        endpoints: &Endpoints,
    ) -> Result<Snapshot, String> {
        let devices = devices::Devices::open(endpoints)?;
        self.start_with(Box::new(devices), limit, meeting_id, debug_id)
    }

    /// Begin a recording from an arbitrary source.
    ///
    /// The seam the tests record through, and the reason pause, budget and
    /// vault can be checked without a microphone.
    ///
    /// # Errors
    ///
    /// See [`Recorder::start`].
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn start_with(
        &self,
        source: Box<dyn Source>,
        limit: Duration,
        meeting_id: &str,
        debug_id: &str,
    ) -> Result<Snapshot, String> {
        // A recording that ended on its own is collected first, or starting
        // the next meeting would be refused by a thread that is already over.
        self.reap();
        let mut guard = self.engine.lock().expect("the recorder lock is poisoned");
        if let Some(running) = guard.as_ref() {
            // Starting twice would abandon the first meeting, which is the one
            // failure a person cannot recover from.
            return Err(format!("already {:?}", running.snapshot().state));
        }
        if self
            .completed
            .lock()
            .expect("the recorder lock is poisoned")
            .is_some()
        {
            return Err("a recording is still waiting to be uploaded".to_owned());
        }

        let vault = self.vault()?;
        // Opened before anything is started, so a store that refuses is a
        // refusal to record rather than a meeting that is lost at the end.
        let recording = vault
            .begin(meeting_id, debug_id, "default input", "default output")
            .map_err(|error| error.to_string())?;
        let writer = SegmentedOpusWriter::with_sink(
            VaultSegmentSink::new(recording),
            BITRATE,
            SEGMENT_SECONDS,
        )
        .map_err(|error| error.to_string())?;

        let engine = Engine::start(
            source,
            writer,
            Session::start(limit),
            vault,
            meeting_id.to_owned(),
            debug_id.to_owned(),
        );
        let snapshot = engine.snapshot();
        *guard = Some(engine);
        Ok(snapshot)
    }

    /// Suspend capture.
    ///
    /// # Errors
    ///
    /// When nothing is being recorded, or it is already paused.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn pause(&self) -> Result<AppState, String> {
        self.reap();
        self.with_engine(Engine::pause)
    }

    /// Carry on.
    ///
    /// # Errors
    ///
    /// When nothing is paused.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn resume(&self) -> Result<AppState, String> {
        self.reap();
        self.with_engine(Engine::resume)
    }

    /// End the recording and hand it to the uploader.
    ///
    /// Waits for the devices to close and the last segment to be written,
    /// because the caller's next question is "can I upload it".
    ///
    /// # Errors
    ///
    /// When nothing is being recorded, or the recording could not be closed.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn finish(&self) -> Result<AppState, String> {
        self.reap();
        let engine = {
            let mut guard = self.engine.lock().expect("the recorder lock is poisoned");
            let engine = guard
                .take()
                .ok_or_else(|| "nothing is being recorded".to_owned())?;
            match engine.stop() {
                Ok(_) => engine,
                Err(message) => {
                    // Refused: put it back rather than leaving the recording
                    // running with nothing holding its handle.
                    *guard = Some(engine);
                    return Err(message);
                }
            }
        };

        let outcome = engine
            .join()
            .map_err(|error: EngineError| error.to_string())?;
        *self
            .completed
            .lock()
            .expect("the recorder lock is poisoned") = Some(outcome);
        Ok(AppState::Uploading)
    }

    /// Take the finished recording, for the uploader.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    #[must_use]
    pub fn take_completed(&self) -> Option<Outcome> {
        self.completed
            .lock()
            .expect("the recorder lock is poisoned")
            .take()
    }

    /// Audio kept so far, which is what gets billed.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    #[must_use]
    pub fn recorded(&self) -> Duration {
        self.snapshot().map_or(Duration::ZERO, |snapshot| {
            Duration::from_millis(snapshot.recorded_ms)
        })
    }

    /// Why the last recording could not be closed.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    #[must_use]
    pub fn fault(&self) -> Option<String> {
        self.reap();
        self.fault
            .lock()
            .expect("the recorder lock is poisoned")
            .clone()
    }

    /// Collect a recording that ended without being asked to.
    ///
    /// The budget of EF-34 and a pair of devices that both die end the thread
    /// on their own. Nobody is holding a return value for those, so the result
    /// is picked up on the next question anybody asks.
    fn reap(&self) {
        let mut guard = self.engine.lock().expect("the recorder lock is poisoned");
        if guard.as_ref().is_none_or(Engine::is_running) {
            return;
        }
        let Some(engine) = guard.take() else {
            return;
        };
        match engine.join() {
            Ok(outcome) => {
                *self
                    .completed
                    .lock()
                    .expect("the recorder lock is poisoned") = Some(outcome);
            }
            Err(error) => {
                *self.fault.lock().expect("the recorder lock is poisoned") =
                    Some(error.to_string());
            }
        }
    }

    /// A handle on the local store.
    fn vault(&self) -> Result<Vault<DpapiSealer>, String> {
        let root = self
            .root
            .as_ref()
            .ok_or_else(|| "NovaBrief has nowhere to record to on this machine".to_owned())?;
        let account = self
            .account
            .as_ref()
            .ok_or_else(|| "NovaBrief has nowhere to record to on this machine".to_owned())?;
        Ok(Vault::new(root, account.clone(), DpapiSealer))
    }

    fn with_engine(
        &self,
        operation: impl FnOnce(&Engine) -> Result<AppState, String>,
    ) -> Result<AppState, String> {
        let guard = self.engine.lock().expect("the recorder lock is poisoned");
        let engine = guard
            .as_ref()
            .ok_or_else(|| "nothing is being recorded".to_owned())?;
        operation(engine)
    }
}

// The commands below are deliberately **not** `pub`.
//
// `#[tauri::command]` generates a `#[macro_export]` helper, which lands at the
// crate root; on a `pub fn` at the crate root the macro's own `pub use` then
// collides with it - `the name __cmd__current_state is defined multiple times`.
// They are only ever reached through `generate_handler!` in this module, so
// private is both correct and what the error was asking for.

/// What the UI needs to draw itself.
#[tauri::command]
fn current_state(recorder: tauri::State<'_, Recorder>) -> AppState {
    recorder.state()
}

/// Everything the widget draws, in one reading.
#[tauri::command]
fn snapshot(recorder: tauri::State<'_, Recorder>) -> Option<Snapshot> {
    recorder.snapshot()
}

/// The notification-area menu for the current state.
#[tauri::command]
fn menu(recorder: tauri::State<'_, Recorder>) -> Vec<MenuItem> {
    tray_menu(recorder.state())
}

/// Begin a recording. `limit_seconds` comes from the plan, never from here.
///
/// # Errors
///
/// When a recording is already under way, or the devices cannot be opened.
#[tauri::command]
fn start_recording(
    recorder: tauri::State<'_, Recorder>,
    limit_seconds: u64,
    meeting_id: String,
    debug_id: String,
) -> Result<Snapshot, String> {
    recorder.start(
        Duration::from_secs(limit_seconds),
        &meeting_id,
        &debug_id,
        &Endpoints::default(),
    )
}

/// Suspend capture.
///
/// Named operations rather than one `advance(target)`: the tray should be able
/// to say "pause", not to put the recorder in any state it likes. A generic
/// transition command is an API that lets a UI bug declare a meeting uploaded.
///
/// # Errors
///
/// When nothing is being recorded.
#[tauri::command]
fn pause(recorder: tauri::State<'_, Recorder>) -> Result<AppState, String> {
    recorder.pause()
}

/// Carry on.
///
/// # Errors
///
/// When nothing is paused.
#[tauri::command]
fn resume(recorder: tauri::State<'_, Recorder>) -> Result<AppState, String> {
    recorder.resume()
}

/// End the recording.
///
/// # Errors
///
/// When nothing is being recorded.
#[tauri::command]
fn finish(recorder: tauri::State<'_, Recorder>) -> Result<AppState, String> {
    recorder.finish()
}

/// Milliseconds of audio kept so far - the figure EF-33 says is billed.
#[tauri::command]
fn recorded_ms(recorder: tauri::State<'_, Recorder>) -> u64 {
    recorder.recorded().as_millis() as u64
}

/// Build and run the application.
///
/// # Panics
///
/// If Tauri cannot start, which on Windows means WebView2 is missing - a
/// condition the installer is responsible for and which nothing here can
/// recover from.
pub fn run() {
    tauri::Builder::default()
        .manage(Recorder::new())
        .invoke_handler(tauri::generate_handler![
            current_state,
            snapshot,
            menu,
            start_recording,
            pause,
            resume,
            finish,
            recorded_ms
        ])
        .setup(|app| {
            // The window exists but stays hidden until asked for. Creating it
            // lazily would mean the first open pays for WebView2 startup, which
            // is seconds - and the moment somebody wants it is the moment they
            // are already in a hurry.
            if let Some(window) = app.get_webview_window("main") {
                window.hide()?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("NovaBrief could not start");
}
