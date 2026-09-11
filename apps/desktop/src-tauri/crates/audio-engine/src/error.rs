//! Errors raised by the capture engine.

use std::fmt;

/// Which endpoint an error came from, so that a message can name the stream the
/// user recognises rather than an opaque COM interface.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Endpoint {
    /// Default capture device (the microphone).
    Microphone,
    /// Default render device, captured in loopback (what the user hears).
    SystemLoopback,
}

impl fmt::Display for Endpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Microphone => f.write_str("microphone"),
            Self::SystemLoopback => f.write_str("system loopback"),
        }
    }
}

/// Anything that can go wrong while capturing.
#[derive(Debug, thiserror::Error)]
pub enum CaptureError {
    #[error("COM could not be initialised on this thread: {0}")]
    ComInit(#[source] windows::core::Error),

    #[error("no default {endpoint} device is available")]
    NoDefaultDevice {
        endpoint: Endpoint,
        #[source]
        source: windows::core::Error,
    },

    #[error("the {endpoint} device could not be opened: {source}")]
    DeviceOpen {
        endpoint: Endpoint,
        #[source]
        source: windows::core::Error,
    },

    /// The mix format is negotiated by Windows, so an unsupported one is a gap
    /// in our conversion table rather than a user error. It is reported with
    /// enough detail to add the missing case.
    #[error(
        "the {endpoint} device uses an audio format this build cannot convert \
         ({bits} bit, {channels} channels, {sample_rate} Hz, tag {tag:#06x})"
    )]
    UnsupportedFormat {
        endpoint: Endpoint,
        bits: u16,
        channels: u16,
        sample_rate: u32,
        tag: u16,
    },

    #[error("no active {endpoint} device matches {wanted:?}")]
    DeviceNotFound { endpoint: Endpoint, wanted: String },

    #[error("the audio stream from the {endpoint} failed while recording: {source}")]
    StreamFailure {
        endpoint: Endpoint,
        #[source]
        source: windows::core::Error,
    },

    /// The replacement device negotiated a different format.
    ///
    /// Refused rather than accepted. Everything downstream - the resampler, the
    /// mixer, the stereo layout of EF-31 - was built for the original format,
    /// and feeding it 44 100 Hz where it expects 48 000 would not fail: it
    /// would quietly change the pitch of the second half of the meeting.
    #[error("the {endpoint} device came back as {now:?}, not {was:?}")]
    FormatChanged {
        endpoint: Endpoint,
        was: crate::format::StreamFormat,
        now: crate::format::StreamFormat,
    },
}

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, CaptureError>;
