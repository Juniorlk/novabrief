//! NovaBrief for Windows.
//!
//! The application has no main window. It lives in the notification area and
//! opens a window only when somebody asks for one (EF-12), because the product
//! it competes with is "remember to start the recorder", and an app that wants
//! a window on screen loses that contest before the meeting begins.

pub mod auth;
pub mod devices;
pub mod engine;
pub mod paths;
pub mod queue;
pub mod recording;
pub mod session;
pub mod state;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use api_client::ApiClient;
use audio_engine::encode::SegmentedOpusWriter;
use audio_engine::pipeline::Endpoints;
use auth::{Account, Identified};
use engine::{Engine, EngineError, Outcome, Snapshot, Source};
use queue::{Pending, Queue};
use recording::VaultSegmentSink;
use session::Session;
use state::{tray_menu, AppState, MenuItem};
use tauri::{Emitter, Manager};
use vault::{AccountKey, CredentialStore, DeviceSecret, DpapiSealer, Vault};

/// Where the API lives.
///
/// Not a plan value and not a secret: it is which deployment this build talks
/// to. The override exists so a developer can point at a machine of their own,
/// and it is read once at start-up rather than per request, so a build cannot
/// change endpoint under a running session.
const DEFAULT_API: &str = "https://api.novabrief.cloud";

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

/// What a recording is called, before and after the server has heard of it.
///
/// Two identifiers, deliberately. The local one names the vault directory and
/// is generated here, so a recording can begin with no network at all
/// (ADR-05); the server one arrives from the declaration of EF-40 and is
/// `None` until it does. Collapsing them would mean either refusing to record
/// without a network, or sending audio to a meeting that does not exist.
#[derive(Debug, Clone)]
pub struct Declared {
    /// Names the vault directory. Generated on this machine.
    pub local_id: String,
    /// The meeting the server knows, once it does.
    pub server_meeting_id: Option<String>,
    /// ADR-07. From the server when it answered, local when it did not.
    pub debug_id: String,
    /// When the recording began, RFC 3339.
    pub started_at: String,
}

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
        declared: &Declared,
        endpoints: &Endpoints,
    ) -> Result<Snapshot, String> {
        let devices = devices::Devices::open(endpoints)?;
        self.start_with(Box::new(devices), limit, declared)
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
        declared: &Declared,
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
        let mut recording = vault
            .begin(
                &declared.local_id,
                &declared.debug_id,
                "default input",
                "default output",
            )
            .map_err(|error| error.to_string())?;
        // Written now, not at the end: a laptop that dies mid-meeting and
        // uploads on Monday would otherwise declare Friday as Monday.
        recording
            .note_start(&declared.started_at)
            .map_err(|error| error.to_string())?;
        if let Some(server) = declared.server_meeting_id.as_deref() {
            recording
                .note_server_meeting(server)
                .map_err(|error| error.to_string())?;
        }
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
            declared.local_id.clone(),
            declared.debug_id.clone(),
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
/// EF-40 wants the meeting declared as recording starts, and ADR-05 wants the
/// network to be optional. Both: the declaration is attempted, and a failure
/// is not one - the recording begins under an identifier of this machine and
/// the queue declares it when a network appears.
///
/// # Errors
///
/// When a recording is already under way, or the devices cannot be opened.
/// Never because the server could not be reached.
#[tauri::command]
async fn start_recording(
    recorder: tauri::State<'_, Recorder>,
    desk: tauri::State<'_, Desk>,
    limit_seconds: u64,
    title: Option<String>,
) -> Result<Snapshot, String> {
    let local = local_id();
    let started_at = now();

    let announced = match desk.account.access_token().await {
        Ok(token) => desk
            .client
            .declare_meeting(&token, &started_at, title.as_deref())
            .await
            .ok(),
        Err(_) => None,
    };

    let declared = Declared {
        debug_id: announced
            .as_ref()
            .map_or_else(|| format!("DBG-LOCAL-{local}"), |m| m.debug_id.clone()),
        server_meeting_id: announced.map(|meeting| meeting.id),
        local_id: local,
        started_at,
    };
    recorder.start(
        Duration::from_secs(limit_seconds),
        &declared,
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

/// End the recording, and set the upload going.
///
/// # Errors
///
/// When nothing is being recorded.
#[tauri::command]
fn finish(
    recorder: tauri::State<'_, Recorder>,
    desk: tauri::State<'_, Desk>,
) -> Result<AppState, String> {
    let state = recorder.finish()?;
    // The queue would find it on its next sweep anyway; this is what makes the
    // upload start in the second after the button rather than the minute.
    desk.queue.nudge();
    Ok(state)
}

/// The organization's meetings, newest first.
///
/// Asked of the server rather than assembled from the vault: the vault knows
/// what this machine recorded, and what the person wants to see is what their
/// organization has - including the meeting a colleague recorded and the
/// report that was produced after this laptop was closed.
///
/// # Errors
///
/// When the session is not usable, or the server cannot be reached.
#[tauri::command]
async fn meetings(desk: tauri::State<'_, Desk>) -> Result<Vec<api_client::Meeting>, String> {
    let token = desk.account.access_token().await?;
    desk.client
        .meetings(&token)
        .await
        .map_err(|error| error.to_string())
}

/// One meeting, with its report and transcript once they exist.
///
/// # Errors
///
/// See [`meetings`].
#[tauri::command]
async fn meeting_detail(
    desk: tauri::State<'_, Desk>,
    meeting_id: String,
) -> Result<api_client::MeetingDetail, String> {
    let token = desk.account.access_token().await?;
    desk.client
        .meeting_detail(&token, &meeting_id)
        .await
        .map_err(|error| error.to_string())
}

/// One entry of the notification-area menu, with its label already
/// translated.
///
/// The labels come from the front end because that is where i18n lives
/// (`CLAUDE.md` section 6), and a tray menu built in Rust is exactly where
/// hard-coded French creeps in. `state.rs` still decides which entries exist
/// and which are enabled; this only carries the words.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct TrayLabel {
    /// Matches the identifier `state::tray_menu` hands out.
    pub id: String,
    /// What to draw, in the language the person reads.
    pub label: String,
    /// Whether it can be chosen right now.
    pub enabled: bool,
}

/// Replace the notification-area menu.
///
/// Choosing an entry emits `tray://<id>` rather than acting here. The window
/// already knows how to start, pause and stop - it has buttons that do exactly
/// that - and a second path into the recorder would be a second place for the
/// rules to be applied slightly differently.
///
/// # Errors
///
/// If the tray icon is gone, or Windows refuses the menu.
#[tauri::command]
fn set_tray_menu(app: tauri::AppHandle, items: Vec<TrayLabel>) -> Result<(), String> {
    use tauri::menu::{Menu, MenuItem};

    let built: Vec<MenuItem<tauri::Wry>> = items
        .iter()
        .map(|item| {
            MenuItem::with_id(&app, &item.id, &item.label, item.enabled, None::<&str>)
                .map_err(|error| error.to_string())
        })
        .collect::<Result<_, String>>()?;
    let entries: Vec<&dyn tauri::menu::IsMenuItem<tauri::Wry>> = built
        .iter()
        .map(|item| item as &dyn tauri::menu::IsMenuItem<tauri::Wry>)
        .collect();

    let menu = Menu::with_items(&app, &entries).map_err(|error| error.to_string())?;
    let tray = app
        .tray_by_id("main")
        .ok_or_else(|| "the notification area icon is gone".to_owned())?;
    tray.set_menu(Some(menu)).map_err(|error| error.to_string())
}

/// Put the window on screen (EF-12).
///
/// # Errors
///
/// If the window is gone, which on Windows means the WebView died.
#[tauri::command]
fn show_window(window: tauri::Window) -> Result<(), String> {
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

/// Put it away again. The recording carries on (EF-12).
///
/// # Errors
///
/// See [`show_window`].
#[tauri::command]
fn hide_window(window: tauri::Window) -> Result<(), String> {
    window.hide().map_err(|error| error.to_string())
}

/// What is still owed to the server, and why it has not gone yet.
///
/// # Errors
///
/// Never; the `Result` is what Tauri requires of an async command.
#[tauri::command]
async fn uploads(desk: tauri::State<'_, Desk>) -> Result<Vec<Pending>, String> {
    Ok(desk.queue.status().await)
}

/// Milliseconds of audio kept so far - the figure EF-33 says is billed.
#[tauri::command]
fn recorded_ms(recorder: tauri::State<'_, Recorder>) -> u64 {
    recorder.recorded().as_millis() as u64
}

/// Which API this installation talks to.
#[must_use]
pub fn api_base() -> String {
    std::env::var("NOVABRIEF_API_URL").unwrap_or_else(|_| DEFAULT_API.to_owned())
}

/// The account, as the commands see it.
pub type Signed = Account<Arc<ApiClient>>;

/// Everything the application owns besides the recorder.
///
/// One client, shared. The session renews the token and the upload queue uses
/// it; two clients would each hold a connection pool and, worse, each renew -
/// and a refresh token presented twice revokes the family.
#[derive(Debug)]
pub struct Desk {
    /// Who is signed in.
    pub account: Arc<Signed>,
    /// The API, shared with the account.
    pub client: Arc<ApiClient>,
    /// What is still owed to the server.
    pub queue: Queue,
}

/// A fresh identifier for a recording, valid with no network at all.
fn local_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Now, in the format the API declares meetings in.
fn now() -> String {
    time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        // A clock that cannot be formatted is not a reason to refuse to
        // record. The server fills a missing start with the declaration time,
        // which is wrong by seconds rather than by a meeting.
        .unwrap_or_default()
}

/// Sign in with an email and a password (EF-11).
///
/// # Errors
///
/// When they are wrong, or the session cannot be written to this machine.
#[tauri::command]
async fn sign_in(
    account: tauri::State<'_, Signed>,
    email: String,
    password: String,
) -> Result<Identified, String> {
    account.sign_in(&email, &password).await
}

/// Bring back the session this machine was left with.
///
/// # Errors
///
/// When the stored token is no longer accepted, which means signing in again.
#[tauri::command]
async fn restore_session(account: tauri::State<'_, Signed>) -> Result<Identified, String> {
    account.restore().await
}

/// Who is signed in, without asking the server.
///
/// # Errors
///
/// Never; the `Result` is what Tauri requires of an async command.
#[tauri::command]
async fn session(account: tauri::State<'_, Signed>) -> Result<Option<Identified>, String> {
    Ok(account.identified().await)
}

/// Whether this machine has been linked at all.
///
/// Read from the disk, so the first screen can be drawn before any request.
///
/// # Errors
///
/// Never; the `Result` is what Tauri requires of an async command.
#[tauri::command]
async fn is_linked(account: tauri::State<'_, Signed>) -> Result<bool, String> {
    Ok(account.is_linked())
}

/// Forget the session on this machine.
///
/// # Errors
///
/// When the stored credential cannot be removed.
#[tauri::command]
async fn sign_out(account: tauri::State<'_, Signed>) -> Result<(), String> {
    account.sign_out().await
}

/// The account this installation uses.
///
/// # Panics
///
/// If `NOVABRIEF_API_URL` is set to something credentials must not be sent to.
/// Falling back to the default would be worse: an operator who pointed the
/// build at a staging server would get production without being told.
#[must_use]
pub fn desk(recorder: &Recorder) -> Desk {
    let client = Arc::new(
        ApiClient::new(&api_base())
            .expect("NOVABRIEF_API_URL must be an HTTPS address, or a loopback one"),
    );
    let credentials = paths::data_root().map_or_else(
        |_| PathBuf::from("credentials.bin"),
        |root| paths::credentials_path(&root),
    );
    let account = Arc::new(Account::new(
        Arc::clone(&client),
        CredentialStore::new(credentials, DpapiSealer),
    ));

    let queue = Queue::start(
        Arc::clone(&account),
        Arc::clone(&client),
        recorder.root.clone().unwrap_or_default(),
        recorder.account.clone().unwrap_or_else(|| {
            // A machine with no device secret cannot record either, so the
            // queue has nothing to find. A key that opens nothing is the
            // honest value: every recording it meets is reported unreadable
            // rather than silently skipped.
            AccountKey::derive(b"novabrief/no-device-secret")
        }),
    );

    Desk {
        account,
        client,
        queue,
    }
}

/// Bring the window forward.
///
/// Shown *and* focused: on Windows a window that is merely shown can come up
/// behind the meeting somebody is in, which reads as the click having done
/// nothing.
fn reveal(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
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
        .setup(|app| {
            let recorder = Recorder::new();
            app.manage(desk(&recorder));
            app.manage(recorder);

            if let Some(tray) = app.tray_by_id("main") {
                // A left click opens the window. It is the gesture people try
                // first, and EF-12 is about the software being reachable in
                // one action from wherever they are - which is usually inside
                // the meeting software, not looking for a menu.
                tray.on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click {
                        button: tauri::tray::MouseButton::Left,
                        button_state: tauri::tray::MouseButtonState::Up,
                        ..
                    } = event
                    {
                        reveal(tray.app_handle());
                    }
                });
                tray.on_menu_event(|app, event| {
                    match event.id().as_ref() {
                        // Only the application itself can do these two.
                        "quit" => {
                            app.exit(0);
                            return;
                        }
                        "meetings" | "settings" | "audio-test" => reveal(app),
                        _ => {}
                    }
                    // Everything else is the window's business. It runs even
                    // while hidden, and it is where the rules already are - a
                    // second path into the recorder would be a second place
                    // for them to drift.
                    let _ = app.emit(&format!("tray://{}", event.id().as_ref()), ());
                });
            }

            // The window exists but stays hidden until asked for. Creating it
            // lazily would mean the first open pays for WebView2 startup,
            // which is seconds - and the moment somebody wants it is the
            // moment they are already in a hurry.
            if let Some(window) = app.get_webview_window("main") {
                window.hide()?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            sign_in,
            sign_out,
            session,
            is_linked,
            restore_session,
            current_state,
            snapshot,
            menu,
            start_recording,
            pause,
            resume,
            finish,
            recorded_ms,
            uploads,
            meetings,
            meeting_detail,
            set_tray_menu,
            show_window,
            hide_window
        ])
        .run(tauri::generate_context!())
        .expect("NovaBrief could not start");
}
