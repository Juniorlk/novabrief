//! WASAPI capture of an audio endpoint.
//!
//! One type serves both streams NovaBrief needs:
//!
//! * the **default render device in loopback**, which is what the user hears —
//!   Teams, Meet, Zoom, a browser, anything — captured in shared mode with no
//!   driver, no bot and no elevation;
//! * the **default capture device**, i.e. the microphone.
//!
//! Three behaviours here are deliberate rather than incidental:
//!
//! * **Silence is synthesised.** A loopback client delivers no packet at all
//!   while nothing is playing, so a naive recorder produces a file shorter than
//!   the meeting. The loop tracks how many frames *should* have elapsed against
//!   the wall clock and emits zeroed frames to cover the gap, which keeps the
//!   file continuous and the timeline honest (criterion C4).
//! * **Discontinuities are counted, not hidden.** Windows flags dropped data
//!   with `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY`; the count is reported so
//!   criterion C3 is measured instead of assumed.
//! * **Formats are read, never assumed.** `GetMixFormat` decides; assuming
//!   48 kHz stereo float is how a recorder ends up silent on somebody else's
//!   laptop.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use windows::core::PCWSTR;
use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
use windows::Win32::Foundation::{HANDLE, S_FALSE};
use windows::Win32::Media::Audio::{
    eCapture, eConsole, eRender, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator,
    MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, AUDCLNT_BUFFERFLAGS_SILENT,
    AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, DEVICE_STATE_ACTIVE,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoTaskMemFree, CLSCTX_ALL, COINIT_MULTITHREADED, STGM_READ,
};
use windows::Win32::System::Threading::{
    AvRevertMmThreadCharacteristics, AvSetMmThreadCharacteristicsW,
};

use crate::error::{CaptureError, Endpoint, Result};
use crate::format::{stream_format_from_waveformatex, StreamFormat};

/// WASAPI buffer duration requested at initialisation, in 100 ns units (200 ms).
const REQUESTED_BUFFER_HNS: i64 = 2_000_000;

/// How long the loop sleeps when no packet is ready. Short enough to keep
/// latency low, long enough that polling costs almost no CPU (criterion C7).
const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// Gap that must open up against the wall clock before silence is synthesised.
/// Below this, a missing packet is just jitter and padding it would fight the
/// real data that is about to arrive.
const SILENCE_THRESHOLD: Duration = Duration::from_millis(40);

/// One batch of samples as WASAPI delivered it.
#[derive(Debug)]
pub struct Packet<'a> {
    /// Interleaved samples at the endpoint's native rate and channel count.
    pub samples: &'a [f32],
    /// System-wide performance counter, in 100 ns units, at the instant the
    /// device captured the first frame of this packet.
    ///
    /// This is the only value that makes two endpoints comparable: both devices
    /// report against the same clock, so it says *when* audio happened rather
    /// than when our thread happened to read it. `None` for synthesised silence
    /// and for drivers that do not supply a timestamp.
    pub qpc_100ns: Option<u64>,
    /// When this *stream* started, on the same clock.
    ///
    /// This is what a caller aligning two endpoints needs, and it is not the
    /// timestamp of the first packet: a loopback endpoint stays silent until
    /// something plays, so its first packet can arrive long after the capture
    /// began. The engine reconstructs the real start once and reports it here,
    /// so callers never have to redo — or get wrong — that reasoning.
    pub stream_start_qpc_100ns: Option<u64>,
    /// True when this batch is silence we generated to cover a gap, rather than
    /// audio the device delivered.
    pub synthesised: bool,
}

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
    /// Timestamp of the very first frame this endpoint delivered.
    ///
    /// Two endpoints never start at the same instant — opening a device takes a
    /// different amount of time for each — and how different is a property of
    /// the machine, not a constant. Recording both start timestamps lets the
    /// skew be measured per run and corrected, instead of assumed.
    pub first_qpc_100ns: Option<u64>,
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

/// Initialise COM for the calling thread.
///
/// Every capture runs on its own thread with its own WASAPI client, so each one
/// initialises COM for itself.
fn init_com_for_this_thread() -> Result<()> {
    // SAFETY: initialising COM on this thread. S_FALSE means it was already
    // initialised compatibly, which is fine.
    let hr = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
    if hr.is_err() && hr != S_FALSE {
        return Err(CaptureError::ComInit(windows::core::Error::from(hr)));
    }
    Ok(())
}

/// An opened WASAPI stream, ready to record.
pub struct EndpointCapture {
    client: IAudioClient,
    capture: IAudioCaptureClient,
    format: StreamFormat,
    endpoint: Endpoint,
    device_name: String,
    mmcss_active: bool,
}

impl std::fmt::Debug for EndpointCapture {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EndpointCapture")
            .field("endpoint", &self.endpoint)
            .field("device_name", &self.device_name)
            .field("format", &self.format)
            .field("mmcss_active", &self.mmcss_active)
            .finish_non_exhaustive()
    }
}

impl EndpointCapture {
    /// Open the default render endpoint in loopback mode: what the user hears.
    pub fn open_system_loopback() -> Result<Self> {
        Self::open(Endpoint::SystemLoopback)
    }

    /// Open the default capture endpoint: the microphone.
    pub fn open_microphone() -> Result<Self> {
        Self::open(Endpoint::Microphone)
    }

    /// Open one of the two endpoints, using the Windows default device.
    ///
    /// COM is initialised for the calling thread; the returned value must be
    /// used from that same thread.
    pub fn open(endpoint: Endpoint) -> Result<Self> {
        Self::open_named(endpoint, None)
    }

    /// Open an endpoint, optionally selecting a device by name.
    ///
    /// `wanted` is matched case-insensitively against the device's friendly
    /// name, as a substring, so `--output "Solix"` is enough to pick
    /// "Casque (Solix Nexus ANC)". Passing `None` uses the Windows default.
    pub fn open_named(endpoint: Endpoint, wanted: Option<&str>) -> Result<Self> {
        init_com_for_this_thread()?;

        let loopback = endpoint == Endpoint::SystemLoopback;
        let data_flow = if loopback { eRender } else { eCapture };
        // Loopback is the one case where we capture from a *render* endpoint,
        // which is exactly what the stream flag asks Windows to do.
        let stream_flags = if loopback {
            AUDCLNT_STREAMFLAGS_LOOPBACK
        } else {
            0
        };

        // SAFETY: standard WASAPI enumeration; every pointer below is owned by
        // the COM runtime and released by the wrapper types on drop.
        let (client, capture, format, device_name) = unsafe {
            let enumerator: IMMDeviceEnumerator =
                CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                    .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

            let device = match wanted {
                None => enumerator
                    .GetDefaultAudioEndpoint(data_flow, eConsole)
                    .map_err(|source| CaptureError::NoDefaultDevice { endpoint, source })?,
                Some(name) => find_device_by_name(&enumerator, data_flow, name, endpoint)?,
            };

            let device_name = friendly_name(&device);

            let client: IAudioClient = device
                .Activate(CLSCTX_ALL, None)
                .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

            let mix_format = client
                .GetMixFormat()
                .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

            // Read the negotiated format before anything can fail, then hand the
            // allocation straight back to the COM allocator.
            let parsed = stream_format_from_waveformatex(mix_format, endpoint);
            let init = client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                stream_flags,
                REQUESTED_BUFFER_HNS,
                0,
                mix_format,
                None,
            );
            CoTaskMemFree(Some(mix_format.cast()));

            let format = parsed?;
            init.map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

            let capture: IAudioCaptureClient = client
                .GetService()
                .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

            (client, capture, format, device_name)
        };

        Ok(Self {
            client,
            capture,
            format,
            endpoint,
            device_name,
            mmcss_active: false,
        })
    }

    /// The format Windows negotiated for this endpoint.
    #[must_use]
    pub const fn format(&self) -> StreamFormat {
        self.format
    }

    /// Which endpoint this capture reads from.
    #[must_use]
    pub const fn endpoint(&self) -> Endpoint {
        self.endpoint
    }

    /// Friendly name of the device, as shown in Windows sound settings.
    #[must_use]
    pub fn device_name(&self) -> &str {
        &self.device_name
    }

    /// Whether the capture thread was granted MMCSS "Pro Audio" priority.
    #[must_use]
    pub const fn mmcss_active(&self) -> bool {
        self.mmcss_active
    }

    /// Record until `stop` is set or `max_duration` elapses.
    ///
    /// `on_frames` receives a [`Packet`] of interleaved `f32` samples in the
    /// endpoint's native sample rate and channel count. It is called from this
    /// thread, so it must not block for long: anything slow belongs on the far
    /// side of a queue.
    pub fn record<F>(
        &mut self,
        stop: &AtomicBool,
        max_duration: Option<Duration>,
        mut on_frames: F,
    ) -> Result<CaptureStats>
    where
        F: FnMut(Packet<'_>),
    {
        let priority = ProAudioPriority::acquire();
        self.mmcss_active = priority.is_active();

        // SAFETY: the client is initialised and owned by self.
        unsafe { self.client.Start() }.map_err(|source| CaptureError::StreamFailure {
            endpoint: self.endpoint,
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
        F: FnMut(Packet<'_>),
    {
        let endpoint = self.endpoint;
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
        let rate_hz = self.format.sample_rate.max(1);
        let started = Instant::now();

        while !stop.load(Ordering::Relaxed) {
            if max_duration.is_some_and(|limit| started.elapsed() >= limit) {
                break;
            }

            let mut produced_this_cycle = false;

            loop {
                // SAFETY: capture client is live for as long as self.
                let available = unsafe { self.capture.GetNextPacketSize() }
                    .map_err(|source| CaptureError::StreamFailure { endpoint, source })?;
                if available == 0 {
                    break;
                }

                let mut data: *mut u8 = std::ptr::null_mut();
                let mut frames: u32 = 0;
                let mut flags: u32 = 0;
                let mut qpc: u64 = 0;

                // SAFETY: out-params are live locals; the buffer stays valid
                // until the matching ReleaseBuffer below. The QPC out-param is
                // what makes the two endpoints comparable: it is the system-wide
                // performance counter, in 100 ns units, at the instant the
                // device captured the packet's first frame.
                unsafe {
                    self.capture
                        .GetBuffer(&mut data, &mut frames, &mut flags, None, Some(&mut qpc))
                }
                .map_err(|source| CaptureError::StreamFailure { endpoint, source })?;

                let packet_qpc = (qpc != 0).then_some(qpc);
                if stats.first_qpc_100ns.is_none() {
                    // When the stream begins, this is the timestamp of the first
                    // frame. But a loopback endpoint delivers nothing at all
                    // while nothing is playing, so the first *real* packet can
                    // arrive many seconds in, after that much synthesised
                    // silence. Taking its timestamp as the start of the stream
                    // would claim the capture began when the audio began, which
                    // is a different thing entirely: the two would then be
                    // aligned against each other by that error.
                    //
                    // Every frame already emitted is silence covering the gap,
                    // so subtracting its duration recovers the instant the
                    // capture actually started.
                    stats.first_qpc_100ns = packet_qpc
                        .map(|timestamp| stream_start_from(timestamp, stats.frames, rate_hz));
                }

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
                unsafe { self.capture.ReleaseBuffer(frames) }
                    .map_err(|source| CaptureError::StreamFailure { endpoint, source })?;

                if !decoded.is_empty() {
                    stats.frames += frame_count as u64;
                    produced_this_cycle = true;
                    on_frames(Packet {
                        samples: &decoded,
                        qpc_100ns: packet_qpc,
                        stream_start_qpc_100ns: stats.first_qpc_100ns,
                        synthesised: false,
                    });
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
                    on_frames(Packet {
                        samples: &silence,
                        qpc_100ns: None,
                        stream_start_qpc_100ns: stats.first_qpc_100ns,
                        synthesised: true,
                    });
                } else {
                    std::thread::sleep(POLL_INTERVAL);
                }
            }
        }

        Ok(stats)
    }
}

/// Reconstruct when a stream started, from the timestamp of its first real
/// packet and the number of frames already emitted before that packet.
///
/// A loopback endpoint delivers nothing while nothing is playing, so its first
/// real packet can arrive many seconds into a recording, after that much
/// synthesised silence. Taking that packet's timestamp as the start of the
/// stream confuses "when audio began" with "when capture began" — and two
/// endpoints aligned on that mistake end up shifted by the whole delay.
fn stream_start_from(packet_qpc_100ns: u64, frames_before: u64, sample_rate: u32) -> u64 {
    if sample_rate == 0 {
        return packet_qpc_100ns;
    }
    let elapsed_100ns = (u128::from(frames_before) * 10_000_000) / u128::from(sample_rate);
    packet_qpc_100ns.saturating_sub(u64::try_from(elapsed_100ns).unwrap_or(u64::MAX))
}

/// A selectable audio endpoint, as reported to the user by `--list-devices`.
#[derive(Debug, Clone)]
pub struct DeviceInfo {
    /// Friendly name, as shown in Windows sound settings.
    pub name: String,
    /// Whether Windows currently treats this device as the default.
    pub is_default: bool,
    /// The mix format Windows negotiated, when it could be read.
    pub format: Option<StreamFormat>,
}

impl DeviceInfo {
    /// Whether this endpoint looks like a Bluetooth hands-free (HFP) profile.
    ///
    /// HFP is the profile a headset switches to when its microphone is opened:
    /// the link becomes bidirectional but the audio collapses to mono at 8 or
    /// 16 kHz, which is audibly worse and costs transcription accuracy. Per the
    /// Novafrik arbitration of 2026-09-07 this is **reported, never blocking**.
    ///
    /// Both signals are needed: the name catches the device before its format is
    /// readable, the format catches vendors who do not label the profile.
    #[must_use]
    pub fn is_hands_free(&self) -> bool {
        let name = self.name.to_lowercase();
        let named = name.contains("hands-free")
            || name.contains("hands free")
            || name.contains("mains-libres")
            || name.contains("mains libres");
        let degraded = self
            .format
            .is_some_and(|f| f.channels == 1 && f.sample_rate <= 16_000);
        named || degraded
    }
}

/// List the active endpoints for one direction.
///
/// Enumeration is best-effort reporting: a device that cannot be inspected is
/// listed without its format rather than failing the whole call.
pub fn list_devices(endpoint: Endpoint) -> Result<Vec<DeviceInfo>> {
    init_com_for_this_thread()?;
    let data_flow = if endpoint == Endpoint::SystemLoopback {
        eRender
    } else {
        eCapture
    };

    // SAFETY: standard WASAPI enumeration; the COM wrappers release everything.
    unsafe {
        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

        let default_name = enumerator
            .GetDefaultAudioEndpoint(data_flow, eConsole)
            .ok()
            .map(|device| friendly_name(&device));

        let collection = enumerator
            .EnumAudioEndpoints(data_flow, DEVICE_STATE_ACTIVE)
            .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;
        let count = collection
            .GetCount()
            .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

        let mut devices = Vec::with_capacity(count as usize);
        for index in 0..count {
            let Ok(device) = collection.Item(index) else {
                continue;
            };
            let name = friendly_name(&device);
            let format = device
                .Activate::<IAudioClient>(CLSCTX_ALL, None)
                .ok()
                .and_then(|client| client.GetMixFormat().ok())
                .and_then(|mix| {
                    let parsed = stream_format_from_waveformatex(mix, endpoint).ok();
                    CoTaskMemFree(Some(mix.cast()));
                    parsed
                });

            devices.push(DeviceInfo {
                is_default: default_name.as_deref() == Some(name.as_str()),
                name,
                format,
            });
        }
        Ok(devices)
    }
}

/// Find an active endpoint whose friendly name contains `wanted`.
///
/// # Safety
/// `enumerator` must be a live `IMMDeviceEnumerator`.
unsafe fn find_device_by_name(
    enumerator: &IMMDeviceEnumerator,
    data_flow: windows::Win32::Media::Audio::EDataFlow,
    wanted: &str,
    endpoint: Endpoint,
) -> Result<windows::Win32::Media::Audio::IMMDevice> {
    let needle = wanted.to_lowercase();
    // SAFETY: enumeration over a live collection; items are COM-owned.
    unsafe {
        let collection = enumerator
            .EnumAudioEndpoints(data_flow, DEVICE_STATE_ACTIVE)
            .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;
        let count = collection
            .GetCount()
            .map_err(|source| CaptureError::DeviceOpen { endpoint, source })?;

        for index in 0..count {
            let Ok(device) = collection.Item(index) else {
                continue;
            };
            if friendly_name(&device).to_lowercase().contains(&needle) {
                return Ok(device);
            }
        }
    }

    Err(CaptureError::DeviceNotFound {
        endpoint,
        wanted: wanted.to_owned(),
    })
}

/// Read the device's friendly name, falling back to a placeholder.
///
/// The name is reporting metadata only, so a failure here must never stop a
/// recording that would otherwise work.
///
/// # Safety
/// `device` must be a live `IMMDevice`.
unsafe fn friendly_name(device: &windows::Win32::Media::Audio::IMMDevice) -> String {
    const UNKNOWN: &str = "unknown device";
    // SAFETY: the property store and the variant are released by their wrappers.
    unsafe {
        let Ok(store) = device.OpenPropertyStore(STGM_READ) else {
            return UNKNOWN.to_owned();
        };
        let Ok(value) = store.GetValue(&PKEY_Device_FriendlyName) else {
            return UNKNOWN.to_owned();
        };
        let name = value.to_string();
        if name.is_empty() {
            UNKNOWN.to_owned()
        } else {
            name
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{stream_start_from, CaptureStats};
    use std::time::Duration;

    /// One second expressed in the 100 ns units WASAPI reports.
    const ONE_SECOND: u64 = 10_000_000;

    #[test]
    fn stream_start_equals_the_packet_time_when_nothing_preceded_it() {
        assert_eq!(
            stream_start_from(50 * ONE_SECOND, 0, 48_000),
            50 * ONE_SECOND
        );
    }

    #[test]
    fn stream_start_backs_out_the_silence_synthesised_before_the_first_packet() {
        // A loopback endpoint that stayed silent for nine seconds: 9 s of
        // synthesised frames at 48 kHz, then a real packet at t = 50 s. The
        // stream itself started at t = 41 s, not at t = 50 s.
        let frames_before = 9 * 48_000;
        assert_eq!(
            stream_start_from(50 * ONE_SECOND, frames_before, 48_000),
            41 * ONE_SECOND
        );
    }

    #[test]
    fn stream_start_is_independent_of_the_sample_rate() {
        // The same nine seconds of silence, at a different rate, must back out
        // the same nine seconds.
        let at_16k = stream_start_from(50 * ONE_SECOND, 9 * 16_000, 16_000);
        let at_44k = stream_start_from(50 * ONE_SECOND, 9 * 44_100, 44_100);
        assert_eq!(at_16k, 41 * ONE_SECOND);
        assert_eq!(at_44k, 41 * ONE_SECOND);
    }

    #[test]
    fn stream_start_saturates_rather_than_underflowing() {
        // More silence than the clock has counted cannot wrap around.
        assert_eq!(stream_start_from(ONE_SECOND, 100 * 48_000, 48_000), 0);
    }

    #[test]
    fn stream_start_tolerates_an_unknown_sample_rate() {
        assert_eq!(stream_start_from(7 * ONE_SECOND, 1_000, 0), 7 * ONE_SECOND);
    }

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
