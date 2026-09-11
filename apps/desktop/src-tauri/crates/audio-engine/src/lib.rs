//! Audio capture engine for the NovaBrief Windows client.
//!
//! Scope, as fixed by `docs/tasks/01_POC1_capture_audio.md`: capture the default
//! input device and the default render device (WASAPI loopback) simultaneously,
//! resample both to 16 kHz mono, compensate the drift between the two clocks,
//! and encode them as a stereo Opus stream (left = microphone, right = system)
//! written in 5 to 10 second Ogg segments alongside a JSON manifest.
//!
//! Progress against the three gates Novafrik set:
//!
//! 1. **Loopback alone to a valid WAV** — done.
//! 2. **Microphone + loopback, resampled to 16 kHz and mixed to stereo** — done.
//! 3. **60 minutes continuous with drift under 40 ms** — the mixer measures the
//!    imbalance between the two streams; the compensator that corrects it, then
//!    Opus encoding, segments and the manifest, come next.

pub mod drift;
pub mod encode;
pub mod error;
pub mod format;
pub mod resample;

#[cfg(windows)]
pub mod capture;

pub use drift::{projected_offset_ms, relative_ppm, DriftEstimate, DriftEstimator, DriftRefusal};
pub use encode::{EncodeError, Manifest, SegmentRecord, SegmentedOpusWriter};
pub use error::{CaptureError, Endpoint, Result};
pub use format::{downmix_to_mono, peak, SampleFormat, StreamFormat};
pub use resample::{MonoResampler, StereoMixer, TARGET_SAMPLE_RATE};

#[cfg(windows)]
pub use capture::{list_devices, CaptureStats, DeviceInfo, EndpointCapture, Packet};
