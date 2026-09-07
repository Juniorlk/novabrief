//! `nb-testsignal` — plays clicks every 10 s on the default render device so
//! that `tools/measure_drift.py` can measure the microphone/system offset of a
//! capture produced by `nb-capture` (criterion C2 of POC #1).
#![allow(clippy::print_stdout)]

use std::process::ExitCode;

fn main() -> ExitCode {
    eprintln!(
        "nb-testsignal is delivered by POC #1; the bootstrap commit only \
         registers the crate. See docs/tasks/01_POC1_capture_audio.md."
    );
    ExitCode::FAILURE
}
