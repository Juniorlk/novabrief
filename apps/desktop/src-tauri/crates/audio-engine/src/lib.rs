//! Audio capture engine for the NovaBrief Windows client.
//!
//! Scope, as fixed by `docs/tasks/01_POC1_capture_audio.md`: capture the default
//! input device and the default render device (WASAPI loopback) simultaneously,
//! resample both to 16 kHz mono, compensate the drift between the two clocks,
//! and encode them as a stereo Opus stream (left = microphone, right = system)
//! written in 5 to 10 second Ogg segments alongside a JSON manifest.
//!
//! Milestone reached so far: loopback capture of the default render device to a
//! valid WAV file, with the format read from `GetMixFormat` rather than assumed,
//! silence synthesised while nothing plays, and dropped packets counted.
//! Microphone capture, drift compensation and Opus encoding follow.

pub mod error;
pub mod format;

#[cfg(windows)]
pub mod loopback;

pub use error::{CaptureError, Endpoint, Result};
pub use format::{downmix_to_mono, peak, SampleFormat, StreamFormat};

#[cfg(windows)]
pub use loopback::{CaptureStats, LoopbackCapture};
