//! Audio capture engine for the NovaBrief Windows client.
//!
//! Scope, as fixed by `docs/tasks/01_POC1_capture_audio.md`: capture the default
//! input device and the default render device (WASAPI loopback) simultaneously,
//! resample both to 16 kHz mono, compensate the drift between the two clocks,
//! and encode them as a stereo Opus stream (left = microphone, right = system)
//! written in 5-second Ogg segments alongside a JSON manifest.
//!
//! The bootstrap commit only registers the crate in the workspace so that
//! `cargo fmt`, `cargo clippy` and `cargo test` have a target. The engine itself
//! is written during POC #1.

#![cfg_attr(not(windows), allow(dead_code))]
