//! `nb-capture` — command-line recorder used to validate POC #1.
//!
//! Target usage (see `docs/tasks/01_POC1_capture_audio.md`):
//! `nb-capture --duration 60 --out capture.ogg [--input <device>] [--output <device>]`
//!
//! Printing to stdout is the point of this binary: it displays live text VU
//! meters for both channels while recording.
#![allow(clippy::print_stdout)]

use std::process::ExitCode;

fn main() -> ExitCode {
    eprintln!(
        "nb-capture is delivered by POC #1; the bootstrap commit only registers \
         the crate. See docs/tasks/01_POC1_capture_audio.md."
    );
    ExitCode::FAILURE
}
