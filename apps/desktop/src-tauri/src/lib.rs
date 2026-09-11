//! NovaBrief for Windows.
//!
//! The application has no main window. It lives in the notification area and
//! opens a window only when somebody asks for one (EF-12), because the product
//! it competes with is "remember to start the recorder", and an app that wants
//! a window on screen loses that contest before the meeting begins.

pub mod state;

use std::sync::Mutex;

use state::{tray_menu, AppState, MenuItem};
use tauri::Manager;

/// What the application is doing, shared between the tray and any open window.
#[derive(Debug, Default)]
pub struct Recorder {
    state: Mutex<AppState>,
}

impl Recorder {
    /// The current state.
    ///
    /// # Panics
    ///
    /// If a previous holder of the lock panicked. Nothing here can panic while
    /// holding it - the guarded value is a `Copy` enum - so a poisoned lock
    /// means the process is already unsound and carrying on would hide that.
    #[must_use]
    pub fn state(&self) -> AppState {
        *self.state.lock().expect("the recorder lock is poisoned")
    }

    /// Move to `target`, or refuse.
    ///
    /// The only way the state changes, which is what keeps the illegal moves to
    /// exactly those absent from the table.
    ///
    /// # Errors
    ///
    /// Returns the refused transition when the table does not allow it.
    ///
    /// # Panics
    ///
    /// See [`Recorder::state`].
    pub fn advance(&self, target: AppState) -> Result<AppState, String> {
        let mut guard = self.state.lock().expect("the recorder lock is poisoned");
        if !guard.may_move_to(target) {
            return Err(format!("cannot move from {:?} to {target:?}", *guard));
        }
        *guard = target;
        Ok(target)
    }
}

impl AppState {
    /// `Idle` by default: the app starts recording nothing.
    const fn default_state() -> Self {
        Self::Idle
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::default_state()
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

/// Ask for a transition. Refusals come back as errors rather than being
/// silently ignored, so a UI bug is visible instead of merely inert.
///
/// # Errors
///
/// When the transition is not in the table.
#[tauri::command]
fn advance(recorder: tauri::State<'_, Recorder>, target: AppState) -> Result<AppState, String> {
    recorder.advance(target)
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
        .invoke_handler(tauri::generate_handler![current_state, menu, advance])
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

    #[test]
    fn a_new_recorder_is_idle() {
        assert_eq!(Recorder::default().state(), AppState::Idle);
    }

    #[test]
    fn a_legal_move_is_applied() {
        let recorder = Recorder::default();
        assert_eq!(
            recorder.advance(AppState::Recording),
            Ok(AppState::Recording)
        );
        assert_eq!(recorder.state(), AppState::Recording);
    }

    /// A refused move leaves the state alone. Applying it and reporting an
    /// error would be worse than either.
    #[test]
    fn a_refused_move_changes_nothing() {
        let recorder = Recorder::default();
        assert!(recorder.advance(AppState::Processing).is_err());
        assert_eq!(recorder.state(), AppState::Idle);
    }

    #[test]
    fn the_error_names_both_ends_of_the_refused_move() {
        let recorder = Recorder::default();
        let message = recorder
            .advance(AppState::Uploading)
            .expect_err("Idle cannot upload");
        assert!(message.contains("Idle"), "{message}");
        assert!(message.contains("Uploading"), "{message}");
    }
}
