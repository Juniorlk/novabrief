//! WASAPI loopback capture of the default render device.
//!
//! Shared mode, no driver, no elevation: we open the endpoint the user is
//! already listening to and ask Windows for a copy of the mix. That is what
//! makes NovaBrief independent of Teams, Meet or Zoom.
//!
//! Two behaviours here are deliberate rather than incidental:
//!
//! * **Silence is synthesised.** A loopback client delivers no packet at all
//!   while nothing is playing, so a naive recorder produces a file shorter than
//!   the meeting. The loop tracks how many frames *should* have elapsed against
//!   the wall clock and emits zeroed frames to cover the gap, which keeps the
//!   file continuous and the timeline honest (criterion C4).
//! * **Discontinuities are counted, not hidden.** Windows flags dropped data
//!   with `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY`; the count is reported so
//!   criterion C3 is measured instead of assumed.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use windows::core::PCWSTR;
use windows::Win32::Foundation::{HANDLE, S_FALSE};
use windows::Win32::Media::Audio::{
    eConsole, eRender, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator, MMDeviceEnumerator,
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
    AUDCLNT_STREAMFLAGS_LOOPBACK,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoTaskMemFree, CLSCTX_ALL, COINIT_MULTITHREADED,
};
use windows::Win32::System::Threading::{
    AvRevertMmThreadCharacteristics, AvSetMmThreadCharacteristicsW,
};

use crate::error::{CaptureError, Endpoint, Result};
use crate::format::{stream_format_from_waveformatex, StreamFormat};

const ENDPOINT: Endpoint = Endpoint::SystemLoopback;

/// WASAPI buffer duration requested at initialisation, in 100 ns units (200 ms).
const REQUESTED_BUFFER_HNS: i64 = 2_000_000;

/// How long the loop sleeps when no packet is ready. Short enough to keep
/// latency low, long enough that polling costs almost no CPU (criterion C7).
const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// Gap that must open up against the wall clock before silence is synthesised.
/// Below this, a missing packet is just jitter and padding it would fight the
/// real data that is about to arrive.
const SILENCE_THRESHOLD: Duration = Duration::from_millis(40);

/// What the capture observed, so the POC criteria can be measured rather than
/// declared.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CaptureStats {
    /// Frames handed to the callback, synthesised silence included.
    pub frames: u64,
    /// Packets Windows flagged as following a gap in the stream (C3).
    pub discontinuities: u64,
    /// Packets Windows flagged as digital silence.
    pub silent_packets: u64,
    /// Frames of silence we synthesised because no packet arrived (C4).
    pub padded_frames: u64,
}

impl CaptureStats {
    /// Duration actually written, derived from the frame count.
    #[must_use]
    pub fn duration(&self, sample_rate: u32) -> Duration {
        if sample_rate == 0 {
            return Duration::ZERO;
        }
        Duration::from_secs_f64(self.frames as f64 / f64::from(sample_rate))
    }
}

/// Raises the calling thread to the MMCSS "Pro Audio" class for the duration of
/// the capture, so a busy CPU does not cost us audio frames.
///
/// Failure is not fatal: the capture still works, it is simply more exposed to
/// scheduling jitter under load, so the caller is told rather than stopped.
struct ProAudioPriority(Option<HANDLE>);

impl ProAudioPriority {
    fn acquire() -> Self {
        let mut task_index: u32 = 0;
        // SAFETY: the string is a valid NUL-terminated UTF-16 literal and
        // task_index is a live local for the duration of the call.
        let handle = unsafe {
            AvSetMmThreadCharacteristicsW(
                PCWSTR(windows::core::w!("Pro Audio").as_ptr()),
                &mut task_index,
            )
        };
        Self(handle.ok())
    }

    fn is_active(&self) -> bool {
        self.0.is_some()
    }
}

impl Drop for ProAudioPriority {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            // SAFETY: the handle came from AvSetMmThreadCharacteristicsW and is
            // reverted exactly once.
            let _ = unsafe { AvRevertMmThreadCharacteristics(handle) };
        }
    }
}

/// An opened loopback stream, ready to record.
pub struct LoopbackCapture {
    client: IAudioClient,
    capture: IAudioCaptureClient,
    format: StreamFormat,
    mmcss_active: bool,
}

impl std::fmt::Debug for LoopbackCapture {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LoopbackCapture")
            .field("format", &self.format)
            .field("mmcss_active", &self.mmcss_active)
            .finish_non_exhaustive()
    }
}

impl LoopbackCapture {
    /// Open the default render endpoint in loopback mode.
    ///
    /// COM is initialised for the calling thread; the returned value must be
    /// used from that same thread.
    pub fn open() -> Result<Self> {
        // SAFETY: initialising COM on this thread. S_FALSE means it was already
        // initialised compatibly, which is fine.
        let hr = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
        if hr.is_err() && hr != S_FALSE {
            return Err(CaptureError::ComInit(windows::core::Error::from(hr)));
        }

        // SAFETY: standard WASAPI enumeration; every pointer below is owned by
        // the COM runtime and released by the wrapper types on drop.
        let (client, capture, format) = unsafe {
            let enumerator: IMMDeviceEnumerator =
                CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|source| {
                    CaptureError::DeviceOpen {
                        endpoint: ENDPOINT,
                        source,
                    }
                })?;

            let device = enumerator
                .GetDefaultAudioEndpoint(eRender, eConsole)
                .map_err(|source| CaptureError::NoDefaultDevice {
                    endpoint: ENDPOINT,
                    source,
                })?;

            let client: IAudioClient =
                device
                    .Activate(CLSCTX_ALL, None)
                    .map_err(|source| CaptureError::DeviceOpen {
                        endpoint: ENDPOINT,
                        source,
                    })?;

            let mix_format = client
                .GetMixFormat()
                .map_err(|source| CaptureError::DeviceOpen {
                    endpoint: ENDPOINT,
                    source,
                })?;

            // Read the negotiated format before anything can fail, then hand the
            // allocation straight back to the COM allocator.
            let parsed = stream_format_from_waveformatex(mix_format, ENDPOINT);
            let init = client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                REQUESTED_BUFFER_HNS,
                0,
                mix_format,
                None,
            );
            CoTaskMemFree(Some(mix_format.cast()));

            let format = parsed?;
            init.map_err(|source| CaptureError::DeviceOpen {
                endpoint: ENDPOINT,
                source,
            })?;

            let capture: IAudioCaptureClient =
                client
                    .GetService()
                    .map_err(|source| CaptureError::DeviceOpen {
                        endpoint: ENDPOINT,
                        source,
                    })?;

            (client, capture, format)
        };

        Ok(Self {
            client,
            capture,
            format,
            mmcss_active: false,
        })
    }

    /// The format Windows negotiated for this endpoint.
    #[must_use]
    pub const fn format(&self) -> StreamFormat {
        self.format
    }

    /// Whether the capture thread was granted MMCSS "Pro Audio" priority.
    #[must_use]
    pub const fn mmcss_active(&self) -> bool {
        self.mmcss_active
    }

    /// Record until `stop` is set or `max_duration` elapses.
    ///
    /// `on_frames` receives interleaved `f32` samples in the endpoint's native
    /// sample rate and channel count. It is called from this thread, so it must
    /// not block for long: anything slow belongs on the far side of a queue.
    pub fn record<F>(
        &mut self,
        stop: &AtomicBool,
        max_duration: Option<Duration>,
        mut on_frames: F,
    ) -> Result<CaptureStats>
    where
        F: FnMut(&[f32]),
    {
        let priority = ProAudioPriority::acquire();
        self.mmcss_active = priority.is_active();

        // SAFETY: the client is initialised and owned by self.
        unsafe { self.client.Start() }.map_err(|source| CaptureError::StreamFailure {
            endpoint: ENDPOINT,
            source,
        })?;

        let result = self.pump(stop, max_duration, &mut on_frames);

        // Stop the stream even if the pump failed, so the endpoint is released.
        // SAFETY: pairs with the Start above.
        let _ = unsafe { self.client.Stop() };
        drop(priority);
        result
    }

    fn pump<F>(
        &self,
        stop: &AtomicBool,
        max_duration: Option<Duration>,
        on_frames: &mut F,
    ) -> Result<CaptureStats>
    where
        F: FnMut(&[f32]),
    {
        let mut stats = CaptureStats::default();
        let mut decoded: Vec<f32> = Vec::new();
        let mut silence: Vec<f32> = Vec::new();
        // WASAPI always flags the first packet after Start as discontinuous:
        // there is genuinely no earlier data to be continuous with. Counting it
        // would make C3 fail on every healthy recording, so the first packet is
        // observed and skipped rather than tallied.
        let mut first_packet_seen = false;
        let channels = self.format.channels as usize;
        let rate = f64::from(self.format.sample_rate);
        let started = Instant::now();

        while !stop.load(Ordering::Relaxed) {
            if max_duration.is_some_and(|limit| started.elapsed() >= limit) {
                break;
            }

            let mut produced_this_cycle = false;

            loop {
                // SAFETY: capture client is live for as long as self.
                let available = unsafe { self.capture.GetNextPacketSize() }.map_err(|source| {
                    CaptureError::StreamFailure {
                        endpoint: ENDPOINT,
                        source,
                    }
                })?;
                if available == 0 {
                    break;
                }

                let mut data: *mut u8 = std::ptr::null_mut();
                let mut frames: u32 = 0;
                let mut flags: u32 = 0;

                // SAFETY: out-params are live locals; the buffer stays valid
                // until the matching ReleaseBuffer below.
                unsafe {
                    self.capture
                        .GetBuffer(&mut data, &mut frames, &mut flags, None, None)
                }
                .map_err(|source| CaptureError::StreamFailure {
                    endpoint: ENDPOINT,
                    source,
                })?;

                if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY.0 as u32 != 0 && first_packet_seen
                {
                    stats.discontinuities += 1;
                }
                first_packet_seen = true;

                let frame_count = frames as usize;
                if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 {
                    // Windows says "these bytes are meaningless"; it does not
                    // promise they are zeroed, so we write our own zeros.
                    stats.silent_packets += 1;
                    decoded.clear();
                    decoded.resize(frame_count * channels, 0.0);
                } else if data.is_null() {
                    decoded.clear();
                } else {
                    let byte_len = frame_count * self.format.bytes_per_frame();
                    // SAFETY: WASAPI guarantees `frames * bytes_per_frame` bytes
                    // readable at `data` until ReleaseBuffer.
                    let bytes = unsafe { std::slice::from_raw_parts(data, byte_len) };
                    self.format.decode(bytes, frame_count, &mut decoded);
                }

                // SAFETY: releases exactly the buffer acquired above.
                unsafe { self.capture.ReleaseBuffer(frames) }.map_err(|source| {
                    CaptureError::StreamFailure {
                        endpoint: ENDPOINT,
                        source,
                    }
                })?;

                if !decoded.is_empty() {
                    stats.frames += frame_count as u64;
                    produced_this_cycle = true;
                    on_frames(&decoded);
                }
            }

            if !produced_this_cycle {
                // Nothing is playing: cover the wall-clock gap with silence so
                // the file length keeps matching the meeting length.
                let expected = (started.elapsed().as_secs_f64() * rate) as u64;
                let behind = expected.saturating_sub(stats.frames);
                let threshold = (SILENCE_THRESHOLD.as_secs_f64() * rate) as u64;

                if behind > threshold {
                    let to_pad = usize::try_from(behind).unwrap_or(usize::MAX);
                    silence.clear();
                    silence.resize(to_pad * channels, 0.0);
                    stats.frames += behind;
                    stats.padded_frames += behind;
                    on_frames(&silence);
                } else {
                    std::thread::sleep(POLL_INTERVAL);
                }
            }
        }

        Ok(stats)
    }
}

#[cfg(test)]
mod tests {
    use super::CaptureStats;
    use std::time::Duration;

    #[test]
    fn duration_is_derived_from_the_frame_count() {
        let stats = CaptureStats {
            frames: 48_000,
            ..CaptureStats::default()
        };
        assert_eq!(stats.duration(48_000), Duration::from_secs(1));
    }

    #[test]
    fn duration_of_an_unknown_sample_rate_is_zero_rather_than_a_panic() {
        assert_eq!(CaptureStats::default().duration(0), Duration::ZERO);
    }
}
