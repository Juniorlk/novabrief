//! NovaBrief for Windows.
//!
//! The application has no main window. It lives in the notification area and
//! opens a window only when somebody asks for one (EF-12), because the product
//! it competes with is "remember to start the recorder", and an app that wants
//! a window on screen loses that contest before the meeting begins.

pub mod recording;
pub mod session;
pub mod state;

use std::sync::Mutex;
use std::time::Duration;

use session::Session;
use state::{tray_menu, AppState, MenuItem};
use tauri::Manager;

/// What the application is doing, shared between the tray and any open window.
///
/// `None` is idle: there is no recording, so there is nothing with a budget or
/// a pause state. Modelling it as an absent session rather than an `Idle`
/// variant inside one means nothing can ask a non-existent recording how long
/// it has left.
#[derive(Debug, Default)]
pub struct Recorder {
    session: Mutex<Option<Session>>,
}

impl Recorder {
    /// The current state.
    ///
    /// # Panics
    ///
    /// If a previous holder of the lock panicked. Nothing here can panic while
    /// holding it, so a poisoned lock means the process is already unsound and
    /// carrying on would hide that.
    #[must_use]
    pub fn state(&self) -> AppState {
        self.with(|session| session.map_or(AppState::Idle, Session::state))
    }

    /// Begin a recording that may run for `limit`.
    ///
    /// The limit comes from the caller - lot L5 reads it from the plan - and
    /// never from a constant here (ADR-09). The session clamps it to the
    /// technical ceiling of EF-34.
    ///
    /// # Errors
    ///
    /// When a recording is already under way.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn start(&self, limit: Duration) -> Result<AppState, String> {
        let mut guard = self.session.lock().expect("the recorder lock is poisoned");
        if let Some(existing) = guard.as_ref() {
            return Err(format!("already {:?}", existing.state()));
        }
        let session = Session::start(limit);
        let state = session.state();
        *guard = Some(session);
        Ok(state)
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
        self.act(Session::pause)
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
        self.act(Session::resume)
    }

    /// End the recording and hand it to the uploader.
    ///
    /// # Errors
    ///
    /// When nothing is being recorded.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn finish(&self) -> Result<AppState, String> {
        self.act(Session::finish)
    }

    /// Audio kept so far, which is what gets billed.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    #[must_use]
    pub fn recorded(&self) -> Duration {
        self.with(|session| session.map_or(Duration::ZERO, Session::recorded))
    }

    fn with<T>(&self, read: impl FnOnce(Option<&Session>) -> T) -> T {
        let guard = self.session.lock().expect("the recorder lock is poisoned");
        read(guard.as_ref())
    }

    fn act(
        &self,
        operation: impl FnOnce(&mut Session) -> Result<(), String>,
    ) -> Result<AppState, String> {
        let mut guard = self.session.lock().expect("the recorder lock is poisoned");
        let session = guard
            .as_mut()
            .ok_or_else(|| "nothing is being recorded".to_owned())?;
        operation(session)?;
        Ok(session.state())
    }
}

// The three commands below are deliberately **not** `pub`.
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

/// The notification-area menu for the current state.
#[tauri::command]
fn menu(recorder: tauri::State<'_, Recorder>) -> Vec<MenuItem> {
    tray_menu(recorder.state())
}

/// Begin a recording. `limit_seconds` comes from the plan, never from here.
///
/// # Errors
///
/// When a recording is already under way.
#[tauri::command]
fn start_recording(
    recorder: tauri::State<'_, Recorder>,
    limit_seconds: u64,
) -> Result<AppState, String> {
    recorder.start(Duration::from_secs(limit_seconds))
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
        .manage(Recorder::default())
        .invoke_handler(tauri::generate_handler![
            current_state,
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

#[cfg(test)]
mod tests {
    use super::{AppState, Recorder};
    use std::time::Duration;

    const HOUR: Duration = Duration::from_secs(3600);

    #[test]
    fn a_new_recorder_is_idle() {
        let recorder = Recorder::default();
        assert_eq!(recorder.state(), AppState::Idle);
        assert_eq!(recorder.recorded(), Duration::ZERO);
    }

    #[test]
    fn a_recording_runs_through_its_states() {
        let recorder = Recorder::default();
        assert_eq!(recorder.start(HOUR), Ok(AppState::Recording));
        assert_eq!(recorder.pause(), Ok(AppState::Paused));
        assert_eq!(recorder.resume(), Ok(AppState::Recording));
        assert_eq!(recorder.finish(), Ok(AppState::Uploading));
    }

    /// A refused operation leaves the state alone. Applying it and reporting an
    /// error would be worse than either.
    #[test]
    fn a_refused_operation_changes_nothing() {
        let recorder = Recorder::default();
        assert!(recorder.pause().is_err(), "nothing is being recorded");
        assert_eq!(recorder.state(), AppState::Idle);

        recorder.start(HOUR).expect("starts");
        assert!(recorder.resume().is_err(), "it is not paused");
        assert_eq!(recorder.state(), AppState::Recording);
    }

    /// Starting twice would abandon the first meeting in memory, which is the
    /// one failure a person cannot recover from.
    #[test]
    fn a_second_recording_cannot_displace_the_first() {
        let recorder = Recorder::default();
        recorder.start(HOUR).expect("starts");

        let message = recorder.start(HOUR).expect_err("already recording");
        assert!(message.contains("Recording"), "{message}");
        assert_eq!(recorder.state(), AppState::Recording);
    }

    #[test]
    fn the_error_says_what_was_being_asked_of_nothing() {
        let recorder = Recorder::default();
        let message = recorder.finish().expect_err("nothing to finish");
        assert!(message.contains("nothing is being recorded"), "{message}");
    }
}
