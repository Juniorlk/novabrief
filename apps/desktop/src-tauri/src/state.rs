//! What the application is doing, and what the tray is allowed to offer.
//!
//! A transition table rather than a chain of conditionals, for the same reason
//! the server side uses one (`app/services/meetings.py`): the illegal moves are
//! the ones nobody writes down and therefore nobody tests. Written as data, an
//! illegal move is simply everything absent from the table.
//!
//! This is deliberately *not* the server's state machine. That one tracks a
//! meeting through transcription and analysis; this one tracks a desktop app
//! through a recording, and the two are only loosely coupled - the laptop can
//! be recording while the network is down and the server knows nothing.

use serde::{Deserialize, Serialize};

/// What the desktop is doing right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AppState {
    /// Nothing is being recorded.
    Idle,
    /// Capturing audio.
    Recording,
    /// Capture suspended. EF-33: paused time is not billed, and pausing must
    /// not start a second file.
    Paused,
    /// Recording finished, segments on their way to the server.
    Uploading,
    /// Uploaded; the server is transcribing and analysing.
    Processing,
}

impl AppState {
    /// Where this state may go next.
    #[must_use]
    pub const fn allowed(self) -> &'static [Self] {
        match self {
            Self::Idle => &[Self::Recording],
            // Stopping a recording goes to Uploading, never straight to Idle:
            // audio that was captured is always owed an upload attempt, and a
            // path back to Idle would be a path that silently discards it.
            Self::Recording => &[Self::Paused, Self::Uploading],
            Self::Paused => &[Self::Recording, Self::Uploading],
            // The upload may fail and be retried for a long time (EF-18), but
            // it never falls back into a recording state.
            Self::Uploading => &[Self::Processing, Self::Idle],
            Self::Processing => &[Self::Idle],
        }
    }

    /// Whether this state may move to `target`.
    #[must_use]
    pub fn may_move_to(self, target: Self) -> bool {
        self.allowed().contains(&target)
    }

    /// Whether a recording is in progress, paused included.
    ///
    /// EF-19 turns on this: an update must never install during a recording,
    /// and "recording" there has to include a paused one - the meeting is not
    /// over, and restarting the app would lose it.
    #[must_use]
    pub const fn is_capturing(self) -> bool {
        matches!(self, Self::Recording | Self::Paused)
    }

    /// The i18n key describing this state, which both the tray tooltip and the
    /// window read. One key per state, resolved on the UI side, so no English
    /// or French string is ever built in Rust.
    #[must_use]
    pub const fn label_key(self) -> &'static str {
        match self {
            Self::Idle => "status.idle",
            Self::Recording => "status.recording",
            Self::Paused => "status.paused",
            Self::Uploading => "status.uploading",
            Self::Processing => "status.processing",
        }
    }
}

/// An item in the notification-area menu.
///
/// Carries a key, never a label. `CLAUDE.md` section 6 requires every visible
/// string to go through i18n, and a tray menu built in Rust is exactly where
/// hard-coded French creeps in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct MenuItem {
    /// Stable identifier the front end dispatches on.
    pub id: &'static str,
    /// Translation key for the label.
    pub label_key: &'static str,
    /// Whether it can be chosen in the current state.
    pub enabled: bool,
}

/// The menu as it should look in `state` (EF-12).
///
/// Every item is always present and merely disabled when it does not apply. A
/// menu whose entries appear and disappear moves the ones that remain, and a
/// person who has learned where "Terminer" sits then clicks "Quitter" during a
/// meeting.
#[must_use]
pub fn tray_menu(state: AppState) -> Vec<MenuItem> {
    let capturing = state.is_capturing();
    vec![
        MenuItem {
            id: "start",
            label_key: "tray.start",
            enabled: state == AppState::Idle,
        },
        MenuItem {
            id: "pause",
            label_key: if state == AppState::Paused {
                "tray.resume"
            } else {
                "tray.pause"
            },
            enabled: capturing,
        },
        MenuItem {
            id: "stop",
            label_key: "tray.stop",
            enabled: capturing,
        },
        MenuItem {
            id: "meetings",
            label_key: "tray.meetings",
            enabled: true,
        },
        MenuItem {
            id: "audio-test",
            label_key: "tray.audioTest",
            // A test that opened a second capture while one is running would
            // fight the recording for the device.
            enabled: !capturing,
        },
        MenuItem {
            id: "settings",
            label_key: "tray.settings",
            enabled: true,
        },
        MenuItem {
            id: "quit",
            label_key: "tray.quit",
            enabled: true,
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::{tray_menu, AppState};

    #[test]
    fn every_state_can_be_reached_and_left() {
        let reachable: Vec<AppState> = [
            AppState::Idle,
            AppState::Recording,
            AppState::Paused,
            AppState::Uploading,
            AppState::Processing,
        ]
        .into_iter()
        .filter(|state| {
            [
                AppState::Idle,
                AppState::Recording,
                AppState::Paused,
                AppState::Uploading,
                AppState::Processing,
            ]
            .iter()
            .any(|from| from.may_move_to(*state))
        })
        .collect();

        // Idle is the entry point, so nothing needs to lead to it for the app
        // to start - but everything else must be reachable or it is dead code.
        for state in [
            AppState::Recording,
            AppState::Paused,
            AppState::Uploading,
            AppState::Processing,
        ] {
            assert!(reachable.contains(&state), "{state:?} is unreachable");
        }
        for state in [AppState::Idle, AppState::Recording, AppState::Paused] {
            assert!(!state.allowed().is_empty(), "{state:?} is a dead end");
        }
    }

    /// Audio that was captured is always owed an upload attempt. A path from
    /// Recording straight back to Idle is a path that discards a meeting.
    #[test]
    fn a_recording_cannot_be_dropped_without_an_upload() {
        assert!(!AppState::Recording.may_move_to(AppState::Idle));
        assert!(!AppState::Paused.may_move_to(AppState::Idle));
        assert!(AppState::Recording.may_move_to(AppState::Uploading));
        assert!(AppState::Paused.may_move_to(AppState::Uploading));
    }

    /// EF-19: never update during a recording - and a paused meeting is not
    /// over, so it counts.
    #[test]
    fn a_paused_recording_still_counts_as_recording() {
        assert!(AppState::Paused.is_capturing());
        assert!(AppState::Recording.is_capturing());
        assert!(!AppState::Uploading.is_capturing());
        assert!(!AppState::Idle.is_capturing());
    }

    #[test]
    fn uploading_never_falls_back_into_a_recording_state() {
        assert!(!AppState::Uploading.may_move_to(AppState::Recording));
        assert!(!AppState::Processing.may_move_to(AppState::Recording));
    }

    /// EF-12: the menu keeps its shape so a learned click stays correct.
    #[test]
    fn the_menu_keeps_the_same_items_in_every_state() {
        let shape: Vec<&str> = tray_menu(AppState::Idle).iter().map(|i| i.id).collect();
        for state in [
            AppState::Recording,
            AppState::Paused,
            AppState::Uploading,
            AppState::Processing,
        ] {
            let other: Vec<&str> = tray_menu(state).iter().map(|i| i.id).collect();
            assert_eq!(shape, other, "the menu moved in {state:?}");
        }
    }

    #[test]
    fn starting_is_only_offered_when_nothing_is_running() {
        let enabled = |state: AppState, id: &str| {
            tray_menu(state)
                .into_iter()
                .find(|item| item.id == id)
                .expect("the item exists in every state")
                .enabled
        };

        assert!(enabled(AppState::Idle, "start"));
        assert!(!enabled(AppState::Recording, "start"));
        assert!(!enabled(AppState::Uploading, "start"));

        assert!(enabled(AppState::Recording, "stop"));
        assert!(!enabled(AppState::Idle, "stop"));

        // A second capture would fight the recording for the device.
        assert!(!enabled(AppState::Recording, "audio-test"));
        assert!(enabled(AppState::Idle, "audio-test"));
    }

    #[test]
    fn a_paused_recording_offers_resume_rather_than_pause() {
        let key = |state: AppState| {
            tray_menu(state)
                .into_iter()
                .find(|item| item.id == "pause")
                .expect("the item exists")
                .label_key
        };

        assert_eq!(key(AppState::Recording), "tray.pause");
        assert_eq!(key(AppState::Paused), "tray.resume");
    }

    /// No visible string is built in Rust: the tray hands over keys and the UI
    /// resolves them, so FR and EN stay in one place (CLAUDE.md section 6).
    #[test]
    fn the_tray_carries_keys_rather_than_words() {
        for item in tray_menu(AppState::Idle) {
            assert!(
                item.label_key.starts_with("tray."),
                "{} carries {:?}, which is not a translation key",
                item.id,
                item.label_key
            );
        }
        for state in [AppState::Idle, AppState::Recording, AppState::Paused] {
            assert!(state.label_key().starts_with("status."));
        }
    }
}
