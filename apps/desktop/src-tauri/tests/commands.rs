//! The contract between the commands and what the application manages.
//!
//! Everything underneath the command layer is tested - the rules, the vault,
//! the uploader, the session - and the layer itself had nothing, because it is
//! the one part resolved at **run time**. Tauri matches a command's state
//! arguments to managed types when the command is called, so a command asking
//! for a type nobody managed compiles, links, ships, and fails in front of a
//! person:
//!
//! ```text
//! state not managed for field `account` on command `sign_in`.
//! You must call `.manage()` before using this command
//! ```
//!
//! That is exactly how sign-in shipped broken, and nothing in the suite could
//! have said so.
//!
//! ## Why this reads the source rather than booting the application
//!
//! The honest test is `tauri::test::mock_builder`, which boots the real thing
//! against a mock window and calls every command. It was written, and the test
//! binary does not load on this machine: merely referring to `tauri::test`
//! gives `STATUS_ENTRYPOINT_NOT_FOUND` (0xc0000139) before any test runs - a
//! missing export in a DLL the binary imports, on the only platform this
//! product runs on.
//!
//! So this checks the same contract by reading it: every state type a command
//! asks for must be one `install` puts there. Less elegant, and it runs
//! everywhere including CI, which the other one does not.

const SOURCE: &str = include_str!("../src/lib.rs");

/// Every `tauri::State<'_, T>` a command takes, with the command it is on.
fn state_arguments() -> Vec<(String, String)> {
    let mut found = Vec::new();
    let mut command = String::from("?");

    for line in SOURCE.lines() {
        let trimmed = line.trim();
        for prefix in ["async fn ", "pub fn ", "fn "] {
            if let Some(rest) = trimmed.strip_prefix(prefix) {
                command = rest
                    .split(['(', '<', ' '])
                    .next()
                    .unwrap_or_default()
                    .to_owned();
                break;
            }
        }
        if let Some(rest) = trimmed.split("tauri::State<'_, ").nth(1) {
            if let Some(wanted) = rest.split('>').next() {
                found.push((command.clone(), wanted.trim().to_owned()));
            }
        }
    }
    found
}

/// Every type `install` hands to `manage`.
fn managed() -> Vec<String> {
    let start = SOURCE
        .find("pub fn install<")
        .expect("install is where the state is put");
    let body = &SOURCE[start..];
    let end = body.find("\npub fn run(").unwrap_or(body.len());

    body[..end]
        .lines()
        .filter_map(|line| line.trim().strip_prefix("app.manage("))
        .map(|rest| {
            // The expression is not the type - `app.manage(desk(&recorder))`
            // manages a `Desk` - so the mapping is stated rather than guessed.
            match rest.trim_end_matches(");") {
                "desk(&recorder)" => "Desk".to_owned(),
                "recorder" => "Recorder".to_owned(),
                other => other.to_owned(),
            }
        })
        .collect()
}

/// No command asks for state nobody put there.
#[test]
fn every_command_asks_only_for_state_the_application_manages() {
    let available = managed();
    assert!(
        !available.is_empty(),
        "install manages nothing; the parser or the function has moved"
    );

    let asked = state_arguments();
    assert!(
        !asked.is_empty(),
        "no command takes state; the parser has stopped finding them"
    );

    let missing: Vec<String> = asked
        .iter()
        .filter(|(_, wanted)| !available.contains(wanted))
        .map(|(command, wanted)| format!("{command} asks for {wanted}"))
        .collect();

    assert!(
        missing.is_empty(),
        "these commands fail in the window with \"state not managed\":\n  {}\n\
         managed: {available:?}",
        missing.join("\n  ")
    );
}

/// The two states the application is built around, named so that losing one is
/// a decision rather than an accident.
#[test]
fn the_application_manages_the_recorder_and_the_desk() {
    let available = managed();
    assert!(available.contains(&"Recorder".to_owned()), "{available:?}");
    assert!(available.contains(&"Desk".to_owned()), "{available:?}");
}

/// Every command the window calls is in the handler list.
///
/// A command renamed in Rust and not in `api.ts` is a button that does nothing
/// and says "command not found" inside a WebView, where nobody is looking.
#[test]
fn every_command_the_window_calls_is_registered() {
    const WINDOW: &str = include_str!("../../src/api.ts");

    let start = SOURCE
        .find("tauri::generate_handler![")
        .expect("the handler list");
    let length = SOURCE[start..].find(']').unwrap_or(0);
    let registered = &SOURCE[start..start + length];

    let mut missing = Vec::new();
    for piece in WINDOW.split("invoke(\"").skip(1) {
        let Some(name) = piece.split('"').next() else {
            continue;
        };
        if !registered.contains(name) {
            missing.push(name.to_owned());
        }
    }

    assert!(
        missing.is_empty(),
        "the window calls commands that are not registered: {missing:?}"
    );
}
