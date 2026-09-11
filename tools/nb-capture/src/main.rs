//! `nb-capture` — command-line recorder used to validate POC #1.
//!
//! Captures the microphone and the system audio simultaneously, resamples both
//! to 16 kHz mono, and writes them as a stereo stream where the left channel is
//! the microphone and the right channel is what the machine was playing. The
//! output is either an uncompressed WAV, for analysis, or Opus in segmented Ogg
//! with a manifest, which is what the product ships and is about 32x smaller.
//!
//! Each endpoint runs on its own thread: WASAPI clients are apartment-bound and
//! the two devices have independent clocks, so they cannot share a pump. The
//! threads resample their own stream and hand 16 kHz mono chunks to the writer
//! thread, which interleaves them.
//!
//! Printing to stdout is the point of this binary: it shows live text VU meters
//! for both channels, so a human can see signal arriving on each.
#![allow(clippy::print_stdout)]

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use audio_engine::{
    downmix_to_mono, peak, projected_offset_ms, relative_ppm, CaptureStats, DriftEstimate,
    DriftEstimator, DriftRefusal, Endpoint, EndpointCapture, MonoResampler, SegmentedOpusWriter,
    StereoMixer, TARGET_SAMPLE_RATE,
};
use clap::Parser;

/// How often the VU meters are redrawn.
const METER_REFRESH: Duration = Duration::from_millis(100);

/// Capture a meeting's audio: microphone on the left, system on the right.
#[derive(Debug, Parser)]
#[command(name = "nb-capture", version, about)]
struct Cli {
    /// Recording length in seconds. Omit to record until Ctrl+C.
    #[arg(short, long, value_name = "SECONDS")]
    duration: Option<u64>,

    /// Output file for WAV, or the directory that will hold the segments and
    /// the manifest for Opus.
    #[arg(short, long, value_name = "FILE", default_value = "capture.wav")]
    out: PathBuf,

    /// Record only the microphone, leaving the system channel silent.
    #[arg(long, conflicts_with = "system_only")]
    mic_only: bool,

    /// Record only the system audio, leaving the microphone channel silent.
    #[arg(long)]
    system_only: bool,

    /// Capture device (microphone). Matched as a case-insensitive substring of
    /// the device name; defaults to the Windows default device.
    #[arg(long, value_name = "NAME")]
    input: Option<String>,

    /// Render device to capture in loopback. Matched as a case-insensitive
    /// substring of the device name; defaults to the Windows default device.
    #[arg(long, value_name = "NAME")]
    output: Option<String>,

    /// Output format. `wav` is raw 16 kHz stereo float, useful for analysis
    /// but 128 kB/s. `opus` writes 5-10 s Ogg segments plus a manifest, which
    /// is what the product ships and is roughly 32x smaller.
    #[arg(long, value_enum, default_value_t = Format::Wav)]
    format: Format,

    /// Opus bitrate in bits per second. Ignored for WAV.
    #[arg(long, default_value_t = 32_000, value_name = "BPS")]
    bitrate: u32,

    /// Seconds of audio per Opus segment, clamped to 5-10 s.
    #[arg(long, default_value_t = 5.0, value_name = "SECONDS")]
    segment_seconds: f64,

    /// List the active audio devices and exit.
    #[arg(long)]
    list_devices: bool,

    /// Print one meter line per refresh instead of redrawing in place, so the
    /// output stays readable when piped to a file.
    #[arg(long)]
    no_tty: bool,
}

/// What nb-capture writes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
enum Format {
    /// Uncompressed 16 kHz stereo float: 128 kB/s, for analysis.
    Wav,
    /// Opus in segmented Ogg with a manifest: what the product ships.
    Opus,
}

/// Where mixed stereo audio goes.
///
/// Both variants take the same interleaved stereo samples, so the mixing loop
/// does not need to know which one it is feeding.
enum Sink {
    Wav(Box<hound::WavWriter<std::io::BufWriter<std::fs::File>>>),
    Opus(Box<SegmentedOpusWriter>),
}

impl Sink {
    fn write(&mut self, interleaved: &[f32]) -> Result<()> {
        match self {
            Self::Wav(writer) => {
                for &sample in interleaved {
                    writer.write_sample(sample)?;
                }
                Ok(())
            }
            Self::Opus(writer) => writer.write(interleaved).map_err(Into::into),
        }
    }
}

/// How often the capture prints what it is costing in memory.
///
/// Criterion 2 of the correction brief asks for RAM every five minutes over an
/// hour, and asks for it *during* the run rather than at the end - because the
/// failure it is guarding against is growth, and a single figure at the end
/// cannot tell growth from a high baseline. The 2026-09-07 run reached 668 MB
/// at 32 minutes; a reading at 5, 10 and 15 would have shown the slope long
/// before that.
///
/// Printing it here rather than leaving it to Task Manager is what makes the
/// criterion self-verifying: the evidence ends up in the same transcript as
/// the loss figures.
const MEMORY_REPORT_INTERVAL: Duration = Duration::from_secs(300);

/// Resident memory of this process, in bytes.
///
/// The working set rather than the committed size: it is what the machine is
/// actually being asked to hold, which is the number the criterion is about.
#[cfg(windows)]
fn resident_bytes() -> Option<u64> {
    use windows_sys::Win32::System::ProcessStatus::{
        GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcess;

    let mut counters = PROCESS_MEMORY_COUNTERS {
        cb: std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        ..unsafe { std::mem::zeroed() }
    };
    // SAFETY: `counters` is a live local whose `cb` field declares its own
    // size, which is the contract this call asks for.
    let ok = unsafe {
        GetProcessMemoryInfo(
            GetCurrentProcess(),
            &raw mut counters,
            std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        )
    };
    (ok != 0).then_some(counters.WorkingSetSize as u64)
}

#[cfg(not(windows))]
const fn resident_bytes() -> Option<u64> {
    None
}

/// How far a capture thread may run ahead of the writer.
///
/// The queues used to be unbounded, and that is the second half of the hour-long
/// failure: when the writer fell behind, nothing pushed back, the backlog grew,
/// the quadratic drain made the writer slower still, and memory reached 668 MB.
/// Under that pressure the capture threads missed their deadlines and WASAPI's
/// 200 ms buffer overran - which is where the 10.25 % of audio actually went.
///
/// A chunk is one WASAPI packet resampled to 16 kHz mono, roughly 10 ms. Three
/// hundred of them is about three seconds and a couple of hundred kilobytes:
/// long enough to ride out a disk hiccup, short enough that a writer which has
/// genuinely stopped is noticed in seconds rather than in gigabytes.
///
/// Blocking on a full queue would be worse than dropping. The capture thread
/// has a hard deadline against the device, and making it wait for a slow disk
/// is precisely how frames are lost at the source - where nothing can recover
/// them. Dropping at a counted, reported boundary keeps the loss in a place we
/// can see and measure.
const QUEUE_DEPTH_CHUNKS: usize = 300;

/// A chunk of 16 kHz mono audio on its way to the writer.
struct Chunk {
    samples: Vec<f32>,
    /// Timestamp of the first frame this endpoint ever delivered, repeated on
    /// every chunk so the writer can align the two streams as soon as both have
    /// spoken once.
    first_qpc_100ns: Option<u64>,
}

/// What one capture thread reports back when it finishes.
struct ThreadOutcome {
    endpoint: Endpoint,
    device_name: String,
    input_rate: u32,
    input_channels: u16,
    mmcss: bool,
    stats: CaptureStats,
    /// How fast this device's clock actually ran, or why that could not be
    /// said. A refusal is an answer, and it is printed as one.
    drift: Result<DriftEstimate, DriftRefusal>,
    /// Audio the writer could not keep up with, and which was therefore
    /// dropped rather than silently accumulated.
    overflow: Overflow,
    /// What the device delivered, against what it owed, inside its own
    /// streaming window. This is the C3 measurement.
    delivery: Delivery,
    /// Wall-clock time the capture loop actually ran.
    ///
    /// This is the measuring stick for C3. Windows' discontinuity flag counted
    /// 2 and 5 events while 10.25 % of an hour went missing - it under-reports
    /// by orders of magnitude, because a gap is flagged once however long it
    /// lasts. Frames the device really delivered, against frames its nominal
    /// rate says should have arrived in that much time, cannot be fooled that
    /// way.
    elapsed: Duration,
}

impl ThreadOutcome {
    /// Frames the device really delivered, synthesised silence excluded.
    const fn real_frames(&self) -> u64 {
        self.stats.frames.saturating_sub(self.stats.padded_frames)
    }
}

/// How much audio a device delivered, against how much its own clock says it
/// should have delivered in the same window.
///
/// This is C3, measured with the right instrument. Windows'
/// `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` counted 2 and 5 events while
/// 10.25 % of an hour went missing: a gap is flagged once however long it
/// lasts, so the flag under-reports by orders of magnitude.
///
/// The window runs from the first delivered packet to the last, and that
/// detail is the whole difference between a measurement and an artefact:
///
/// * Opening a device takes time. Counting from the call to `record` charges
///   that startup to the device as lost audio - it showed up as 0.225 % on a
///   clean ten-second capture, which would fail a criterion set at 0.1 %.
/// * A loopback endpoint delivers *nothing at all* while nothing is playing.
///   Against the wall clock a silent machine reports 100 % loss, which is true
///   arithmetic and a useless statement. Between its first and last real
///   packet, it reports what it lost while it was actually streaming.
#[derive(Debug, Clone, Copy, Default)]
struct Delivery {
    /// Timestamp and cumulative frame count at the first delivered packet.
    first: Option<(u64, u64)>,
    /// Same, at the most recent one.
    last: Option<(u64, u64)>,
}

impl Delivery {
    /// Record a packet the device really delivered.
    ///
    /// `frames_before` is the cumulative count *excluding* this packet, so the
    /// window and the frames inside it are bounded by the same two events.
    fn observe(&mut self, qpc_100ns: u64, frames_before: u64) {
        if self.first.is_none() {
            self.first = Some((qpc_100ns, frames_before));
        }
        self.last = Some((qpc_100ns, frames_before));
    }

    /// Seconds between the first and last delivered packet.
    fn window_seconds(&self) -> Option<f64> {
        let (first_qpc, _) = self.first?;
        let (last_qpc, _) = self.last?;
        let span = last_qpc.checked_sub(first_qpc)?;
        Some(span as f64 / 10_000_000.0)
    }

    /// Frames delivered inside that window.
    fn frames_in_window(&self) -> Option<u64> {
        let (_, first_frames) = self.first?;
        let (_, last_frames) = self.last?;
        last_frames.checked_sub(first_frames)
    }

    /// Share of the expected audio that never arrived, as a percentage.
    ///
    /// `None` when the device never streamed long enough to divide by - which
    /// is an answer, not a zero.
    fn shortfall_percent(&self, nominal_rate: u32) -> Option<f64> {
        let seconds = self.window_seconds()?;
        let delivered = self.frames_in_window()? as f64;
        let expected = seconds * f64::from(nominal_rate);
        if expected < 1.0 {
            return None;
        }
        Some(((expected - delivered).max(0.0) / expected) * 100.0)
    }
}

/// What a full queue cost.
#[derive(Debug, Clone, Copy, Default)]
struct Overflow {
    chunks: u64,
    frames: u64,
}

impl Overflow {
    const fn happened(&self) -> bool {
        self.chunks > 0
    }
}

/// How much silence each channel needs so neither can run away from the other.
///
/// Returns `(left, right)` frames of silence to push.
///
/// The mixer only emits frames while **both** sides have data, which is what
/// keeps the two voices aligned. The corollary is that a side which stops
/// delivering pins the other one in memory, and that is not a theory: a
/// 60-minute validation capture had the system loopback stop at minute 28 and
/// the microphone backlog then grew by one second of audio per second of
/// meeting - 6 003 681 frames and 34.8 MB by minute 35, on its way past a
/// gigabyte over a four-hour meeting (EF-34 allows four hours).
///
/// Nothing reported it. The recording carried on, the file kept its duration,
/// and the right channel was simply empty from minute 28 onwards - the same
/// silent, plausible-looking failure as the 10.25 % loss.
///
/// `live` is about the thread, not the configuration. The original code padded
/// a side nobody had asked to record, which is a different condition: it never
/// covered a side that was being recorded and then ended.
#[must_use]
fn silence_needed(imbalance: i64, left_live: bool, right_live: bool) -> (usize, usize) {
    // Both still running: the mixer holds the surplus on purpose, because the
    // counterpart is on its way. That backlog is bounded by the clock
    // difference between two devices, which is milliseconds.
    if left_live && right_live {
        return (0, 0);
    }
    match imbalance.cmp(&0) {
        // Left leads: the right-hand side has stopped, so it is padded.
        std::cmp::Ordering::Greater if !right_live => {
            (0, usize::try_from(imbalance).unwrap_or(usize::MAX))
        }
        std::cmp::Ordering::Less if !left_live => {
            (usize::try_from(-imbalance).unwrap_or(usize::MAX), 0)
        }
        _ => (0, 0),
    }
}

/// What became of an item offered to a full-or-not queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Offered {
    /// The writer took it.
    Taken,
    /// The queue was full. The audio is gone, and counted.
    Dropped {
        /// True the first time only, so the warning is printed once rather
        /// than a hundred times a second.
        first: bool,
    },
    /// The writer is gone; there is nowhere left to send.
    Closed,
}

/// The sending half of a bounded queue, which counts what it had to drop.
///
/// A type rather than three lines inside the capture callback, because this is
/// the behaviour the hour-long failure turned on and it has to be something a
/// test can drive. See `a_producer_faster_than_its_consumer_is_bounded_and_counted`.
#[derive(Debug)]
struct BoundedSink<T> {
    tx: SyncSender<T>,
    overflow: Overflow,
}

impl<T> BoundedSink<T> {
    const fn new(tx: SyncSender<T>) -> Self {
        Self {
            tx,
            overflow: Overflow {
                chunks: 0,
                frames: 0,
            },
        }
    }

    /// Offer one item without ever waiting.
    ///
    /// `frames` is what the item is worth in audio, so the counter measures
    /// lost *audio* rather than lost messages - which is the number anyone
    /// reading the report actually cares about.
    ///
    /// Never blocks, and that is the whole design. The capture thread has a
    /// hard deadline against the device; making it wait on a slow disk is
    /// exactly how frames are lost at the source, where nothing can recover
    /// them. Dropping here is a loss we can count, in a place we can see.
    fn offer(&mut self, item: T, frames: u64) -> Offered {
        match self.tx.try_send(item) {
            Ok(()) => Offered::Taken,
            Err(TrySendError::Full(_)) => {
                self.overflow.chunks += 1;
                self.overflow.frames += frames;
                Offered::Dropped {
                    first: self.overflow.chunks == 1,
                }
            }
            Err(TrySendError::Disconnected(_)) => Offered::Closed,
        }
    }
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    if cli.list_devices {
        return list_all_devices();
    }

    let want_mic = !cli.system_only;
    let want_system = !cli.mic_only;

    let stop = Arc::new(AtomicBool::new(false));
    let ctrlc_flag = Arc::clone(&stop);
    install_ctrlc_handler(move || ctrlc_flag.store(true, Ordering::Relaxed))?;

    let max_duration = cli.duration.map(Duration::from_secs);
    let (mic_tx, mic_rx) = sync_channel::<Chunk>(QUEUE_DEPTH_CHUNKS);
    let (sys_tx, sys_rx) = sync_channel::<Chunk>(QUEUE_DEPTH_CHUNKS);

    let mic_thread = want_mic.then(|| {
        spawn_capture(
            Endpoint::Microphone,
            cli.input.clone(),
            Arc::clone(&stop),
            max_duration,
            mic_tx,
        )
    });
    let sys_thread = want_system.then(|| {
        spawn_capture(
            Endpoint::SystemLoopback,
            cli.output.clone(),
            Arc::clone(&stop),
            max_duration,
            sys_tx,
        )
    });

    println!("Working rate  : {TARGET_SAMPLE_RATE} Hz, stereo (L = microphone, R = system)");
    println!("Output        : {}", cli.out.display());
    match max_duration {
        Some(duration) => println!("Duration      : {} s", duration.as_secs()),
        None => println!("Duration      : until Ctrl+C"),
    }
    println!("\nRecording. Press Ctrl+C to stop.\n");

    let (written, sink, skew_frames) = write_stereo(&cli, &mic_rx, &sys_rx)?;

    // The channels are closed once both capture threads have finished, so the
    // writer above has already drained everything by the time we join.
    let mic_outcome = join_capture(mic_thread)?;
    let sys_outcome = join_capture(sys_thread)?;

    // The manifest records which devices produced the recording, so finalising
    // waits until the capture threads have reported their device names.
    let device_of = |outcome: &Option<ThreadOutcome>| {
        outcome
            .as_ref()
            .map_or_else(|| "not recorded".to_owned(), |o| o.device_name.clone())
    };
    let skew_ms = skew_frames * 1000 / i64::from(TARGET_SAMPLE_RATE);
    let output_path = match cli.format {
        Format::Wav => cli.out.clone(),
        Format::Opus => opus_directory(&cli.out),
    };
    finalize(
        sink,
        &device_of(&mic_outcome),
        &device_of(&sys_outcome),
        skew_ms,
    )?;

    let outcomes = [mic_outcome, sys_outcome];
    report(&output_path, written, &outcomes);
    report_relative_drift(&outcomes);
    Ok(())
}

/// Close the sink, writing the manifest when the output is Opus.
fn finalize(sink: Sink, input_device: &str, output_device: &str, skew_ms: i64) -> Result<()> {
    match sink {
        Sink::Wav(writer) => {
            writer
                .finalize()
                .context("could not finalize the WAV file")?;
            Ok(())
        }
        Sink::Opus(writer) => {
            let manifest = writer
                .finish(input_device, output_device, skew_ms)
                .context("could not finalize the Opus segments")?;
            let bytes: u64 = manifest.segments.iter().map(|s| s.bytes).sum();
            let seconds = manifest.duration_ms as f64 / 1000.0;
            println!(
                "
Opus segments     : {}",
                manifest.segments.len()
            );
            println!("Encoded size      : {bytes} bytes");
            if seconds > 0.0 {
                println!(
                    "Rate              : {:.1} kB/min ({:.1} MB/hour)",
                    bytes as f64 / 1024.0 / (seconds / 60.0),
                    bytes as f64 / 1_048_576.0 / (seconds / 3600.0)
                );
            }
            Ok(())
        }
    }
}

/// Print every active endpoint, flagging Bluetooth hands-free profiles.
fn list_all_devices() -> Result<()> {
    for (endpoint, title) in [
        (Endpoint::Microphone, "Capture devices (microphone)"),
        (Endpoint::SystemLoopback, "Render devices (system loopback)"),
    ] {
        println!("\n{title}");
        let devices = audio_engine::list_devices(endpoint)
            .with_context(|| format!("could not enumerate {endpoint} devices"))?;
        if devices.is_empty() {
            println!("  (none active)");
            continue;
        }
        for device in devices {
            let marker = if device.is_default { "*" } else { " " };
            let format = device.format.map_or_else(
                || "format unavailable".to_owned(),
                |f| {
                    format!(
                        "{} Hz, {} ch, {:?}",
                        f.sample_rate, f.channels, f.sample_format
                    )
                },
            );
            let warning = if device.is_hands_free() {
                "  <- Bluetooth hands-free: degraded audio"
            } else {
                ""
            };
            println!("  {marker} {}\n      {format}{warning}", device.name);
        }
    }
    println!("\n* = Windows default. Select with --input / --output (substring match).");
    Ok(())
}

/// Start one endpoint capture on its own thread, resampling to the working rate.
fn spawn_capture(
    endpoint: Endpoint,
    device: Option<String>,
    stop: Arc<AtomicBool>,
    max_duration: Option<Duration>,
    tx: SyncSender<Chunk>,
) -> std::thread::JoinHandle<Result<ThreadOutcome>> {
    std::thread::spawn(move || -> Result<ThreadOutcome> {
        let mut capture = EndpointCapture::open_named(endpoint, device.as_deref())
            .with_context(|| format!("could not open the {endpoint} endpoint"))?;

        let format = capture.format();
        let mut resampler = MonoResampler::new(format.sample_rate);
        let mut mono = Vec::new();
        let mut converted = Vec::new();
        let mut first_qpc: Option<u64> = None;
        let mut estimator = DriftEstimator::new(format.sample_rate);
        let mut device_frames: u64 = 0;
        let mut delivery = Delivery::default();
        let mut sink = BoundedSink::new(tx);

        let started = Instant::now();
        let stats = capture.record(&stop, max_duration, |packet| {
            // Use the engine's reconstructed stream start, not the raw packet
            // timestamp: for a loopback endpoint the first packet only arrives
            // once something plays, which can be far into the recording.
            if first_qpc.is_none() {
                first_qpc = packet.stream_start_qpc_100ns;
            }

            // Only real packets time the device's own clock. Synthesised
            // silence is generated against the system clock, so feeding it to
            // the estimator would compare that clock with itself and report no
            // drift however far the hardware actually strays.
            let packet_frames =
                (packet.samples.len() / format.channels.max(1) as usize) as u64;
            if packet.synthesised {
                // Counted, never fitted. Synthesised silence is timed by our own
                // clock, so feeding it to the fit would compare that clock with
                // itself - but how much of it there is decides whether the fit
                // on the real frames means anything (D3).
                estimator.note_synthesised(packet_frames);
            } else {
                if let Some(qpc) = packet.qpc_100ns {
                    estimator.observe(qpc, device_frames);
                    delivery.observe(qpc, device_frames);
                }
                device_frames += packet_frames;
            }
            downmix_to_mono(packet.samples, format.channels, &mut mono);
            converted.clear();
            resampler.process(&mono, &mut converted);
            if !converted.is_empty() {
                let frames = converted.len() as u64;
                let chunk = Chunk {
                    samples: std::mem::take(&mut converted),
                    first_qpc_100ns: first_qpc,
                };
                // Counted and announced, never absorbed. The point of bounding
                // the queue is that falling behind becomes a fact somebody
                // reads, instead of memory growing until the capture itself
                // starts failing.
                if let Offered::Dropped { first: true } = sink.offer(chunk, frames) {
                    eprintln!(
                        "\n!! {endpoint}: the writer fell more than {QUEUE_DEPTH_CHUNKS} chunks behind; audio is being dropped"
                    );
                }
            }
        })?;

        Ok(ThreadOutcome {
            endpoint,
            device_name: capture.device_name().to_owned(),
            input_rate: format.sample_rate,
            input_channels: format.channels,
            mmcss: capture.mmcss_active(),
            delivery,
            elapsed: started.elapsed(),
            stats,
            drift: estimator.estimate(),
            overflow: sink.overflow,
        })
    })
}

fn join_capture(
    handle: Option<std::thread::JoinHandle<Result<ThreadOutcome>>>,
) -> Result<Option<ThreadOutcome>> {
    match handle {
        None => Ok(None),
        Some(handle) => match handle.join() {
            Ok(result) => result.map(Some),
            Err(_) => Err(anyhow!("a capture thread panicked")),
        },
    }
}

/// Interleave both streams into a stereo WAV until the capture threads stop.
///
/// Returns the number of stereo frames written.
fn write_stereo(
    cli: &Cli,
    mic_rx: &Receiver<Chunk>,
    sys_rx: &Receiver<Chunk>,
) -> Result<(u64, Sink, i64)> {
    let mut sink = match cli.format {
        Format::Wav => {
            let spec = hound::WavSpec {
                channels: 2,
                sample_rate: TARGET_SAMPLE_RATE,
                bits_per_sample: 32,
                sample_format: hound::SampleFormat::Float,
            };
            Sink::Wav(Box::new(
                hound::WavWriter::create(&cli.out, spec)
                    .with_context(|| format!("could not create {}", cli.out.display()))?,
            ))
        }
        Format::Opus => {
            let directory = opus_directory(&cli.out);
            let prefix = cli.out.file_stem().map_or_else(
                || "capture".to_owned(),
                |s| s.to_string_lossy().into_owned(),
            );
            Sink::Opus(Box::new(
                SegmentedOpusWriter::new(&directory, prefix, cli.bitrate, cli.segment_seconds)
                    .with_context(|| format!("could not open {}", directory.display()))?,
            ))
        }
    };

    let mut mixer = StereoMixer::new();
    let mut interleaved = Vec::new();
    let mut frames: u64 = 0;

    let mic_recorded = !cli.system_only;
    let sys_recorded = !cli.mic_only;
    let mut mic_open = mic_recorded;
    let mut sys_open = sys_recorded;
    let mut mic_peak = 0.0_f32;
    let mut sys_peak = 0.0_f32;
    let mut last_meter = Instant::now();
    let mut last_memory = Instant::now();
    let mut mic_ended_reported = false;
    let mut sys_ended_reported = false;
    let started = Instant::now();
    let mut peak_resident: u64 = 0;
    let mut worst_imbalance: i64 = 0;
    let mut mic_start: Option<u64> = None;
    let mut sys_start: Option<u64> = None;
    let mut applied_skew: Option<i64> = None;

    while mic_open || sys_open {
        let mut received = false;

        // Drain whatever is ready on both sides before writing, so neither
        // channel accumulates a backlog while the other is served. Disconnection
        // is detected here rather than by a second try_recv, which would consume
        // and discard a chunk that arrived in between.
        loop {
            match mic_rx.try_recv() {
                Ok(chunk) => {
                    mic_start = mic_start.or(chunk.first_qpc_100ns);
                    mic_peak = mic_peak.max(peak(&chunk.samples));
                    mixer.push_left(&chunk.samples);
                    received = true;
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    mic_open = false;
                    break;
                }
            }
        }
        loop {
            match sys_rx.try_recv() {
                Ok(chunk) => {
                    sys_start = sys_start.or(chunk.first_qpc_100ns);
                    sys_peak = sys_peak.max(peak(&chunk.samples));
                    mixer.push_right(&chunk.samples);
                    received = true;
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    sys_open = false;
                    break;
                }
            }
        }

        // Correct the start skew exactly once, as soon as both endpoints have
        // reported when their first frame was captured. The two devices never
        // open at the same instant, and how far apart they open is a property
        // of the machine — so it is measured here from the shared performance
        // counter rather than assumed or hard-coded. The stream that started
        // later is missing audio at the front, so it gets that much silence.
        if applied_skew.is_none() && mic_recorded && sys_recorded {
            if let (Some(mic_qpc), Some(sys_qpc)) = (mic_start, sys_start) {
                let delta_100ns = i128::from(mic_qpc) - i128::from(sys_qpc);
                let frames = (delta_100ns * i128::from(TARGET_SAMPLE_RATE)) / 10_000_000;
                let frames = i64::try_from(frames).unwrap_or(0);
                match frames.cmp(&0) {
                    std::cmp::Ordering::Greater => {
                        // The microphone started later: pad its front.
                        mixer.push_front_left(&vec![0.0; frames as usize]);
                    }
                    std::cmp::Ordering::Less => {
                        mixer.push_front_right(&vec![0.0; frames.unsigned_abs() as usize]);
                    }
                    std::cmp::Ordering::Equal => {}
                }
                applied_skew = Some(frames);
            }
        }

        // A side that is not producing - never asked for, or stopped part way
        // through - is fed silence to match the one that is. Without it no
        // stereo frame is ever complete: the file stays empty in the first
        // case, and in the second the live side accumulates in memory for the
        // rest of the meeting.
        let (pad_left, pad_right) = silence_needed(
            mixer.imbalance(),
            mic_recorded && mic_open,
            sys_recorded && sys_open,
        );
        if pad_left > 0 {
            mixer.push_left(&vec![0.0; pad_left]);
        }
        if pad_right > 0 {
            mixer.push_right(&vec![0.0; pad_right]);
        }

        // An endpoint that was being recorded and has stopped is a partial
        // recording, and the person who ran the capture has to be told at the
        // moment it happens rather than at the end.
        if mic_recorded && !mic_open && !mic_ended_reported {
            eprintln!(
                "\n!! the microphone stopped delivering; the rest is silence on the left channel"
            );
            mic_ended_reported = true;
        }
        if sys_recorded && !sys_open && !sys_ended_reported {
            eprintln!(
                "\n!! system audio stopped delivering; the rest is silence on the right channel"
            );
            sys_ended_reported = true;
        }

        // Measure the imbalance only while both endpoints are still running and
        // the start skew has been corrected. Outside that window the number is
        // an artefact, not a measurement: before the correction it is dominated
        // by the startup difference, and after one stream ends it simply grows
        // by however long the other keeps recording. What is left in between is
        // the genuine divergence between the two device clocks.
        let skew_settled = applied_skew.is_some() || !(mic_recorded && sys_recorded);
        let both_running = mic_open && sys_open;
        if skew_settled && both_running && mixer.imbalance().abs() > worst_imbalance.abs() {
            worst_imbalance = mixer.imbalance();
        }

        interleaved.clear();
        mixer.drain_into(&mut interleaved);
        sink.write(&interleaved)
            .context("writing the capture failed mid-recording")?;
        frames += (interleaved.len() / 2) as u64;

        if last_meter.elapsed() >= METER_REFRESH {
            render_meters(mic_peak, sys_peak, cli.no_tty);
            mic_peak = 0.0;
            sys_peak = 0.0;
            last_meter = Instant::now();
        }

        // Criterion 2: the slope matters more than the endpoint, so this prints
        // as the capture runs rather than once at the end.
        if last_memory.elapsed() >= MEMORY_REPORT_INTERVAL {
            if let Some(bytes) = resident_bytes() {
                peak_resident = peak_resident.max(bytes);
                // The imbalance, named for what it is. It is a signed frame
                // count - how far the leading side is ahead of the other -
                // and calling it a queue depth, as this line first did, would
                // have put a wrong unit next to a right number.
                println!(
                    "
[{:>5.1} min] resident memory: {:.1} MB (mixer imbalance: {:+} frames)",
                    started.elapsed().as_secs_f64() / 60.0,
                    bytes as f64 / 1_048_576.0,
                    mixer.imbalance(),
                );
            }
            last_memory = Instant::now();
        }

        if !received && (mic_open || sys_open) {
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    // Nothing more will arrive: emit the tail, padding the shorter side.
    interleaved.clear();
    mixer.flush_into(&mut interleaved);
    sink.write(&interleaved)
        .context("writing the capture tail failed")?;
    frames += (interleaved.len() / 2) as u64;

    let to_ms = |f: i64| f as f64 * 1000.0 / f64::from(TARGET_SAMPLE_RATE);
    println!();
    match applied_skew {
        Some(skew) => println!(
            "\nStart skew (measured from the device clock): {skew} frames ({:.1} ms), corrected",
            to_ms(skew)
        ),
        None => println!("\nStart skew: not measured (only one endpoint recorded)"),
    }
    println!(
        "Worst divergence while both streams ran   : {worst_imbalance} frames ({:.1} ms)",
        to_ms(worst_imbalance)
    );
    // The high-water mark, which is the figure criterion 2 is written against.
    // Sampled every five minutes rather than continuously: a peak between two
    // samples would be missed, but the failure this guards against is steady
    // growth, which no sampling interval can hide.
    let final_resident = resident_bytes().unwrap_or(0);
    peak_resident = peak_resident.max(final_resident);
    if peak_resident > 0 {
        println!(
            "Peak resident memory (C2 budget 50 MB)    : {:.1} MB",
            peak_resident as f64 / 1_048_576.0
        );
    }
    Ok((frames, sink, applied_skew.unwrap_or(0)))
}

/// Directory that holds the Opus segments and their manifest.
///
/// `--out meeting.opus` and `--out meeting` both write into `meeting/`, so the
/// segments never collide with an unrelated file.
fn opus_directory(out: &Path) -> PathBuf {
    out.with_extension("")
}

/// Print the C2 verdict: how far apart the two clocks run, and what that means
/// for a meeting of realistic length.
fn report_relative_drift(outcomes: &[Option<ThreadOutcome>]) {
    let drifts: Vec<DriftEstimate> = outcomes
        .iter()
        .flatten()
        .filter_map(|outcome| outcome.drift.as_ref().ok().copied())
        .collect();

    let [mic, system] = drifts.as_slice() else {
        // Name what is missing on each endpoint. "Not measurable" on its own
        // sent the reader looking for a bug; "8.3 % of frames were synthesised"
        // sends them to the actual problem, which is C3 and not C2.
        println!(
            "
--- Clock drift (C2) ---"
        );
        println!("Relative drift    : not measurable");
        for outcome in outcomes.iter().flatten() {
            if let Err(reason) = &outcome.drift {
                println!("  {:<16}: {reason}", outcome.endpoint.to_string());
            }
        }
        return;
    };

    let ppm = relative_ppm(mic, system);
    let shortest = mic.observed_seconds.min(system.observed_seconds);
    println!(
        "
--- Clock drift (C2) ---"
    );
    println!("Relative drift    : {ppm:+.2} ppm (microphone against system)");
    println!("Measured over     : {shortest:.0} s of real packets on both endpoints");
    for minutes in [15.0_f64, 60.0, 90.0] {
        println!(
            "  projected at {:>2.0} min : {:+7.1} ms",
            minutes,
            projected_offset_ms(ppm, minutes * 60.0)
        );
    }
    // C2 allows 40 ms after 60 minutes, which is 11.1 ppm of relative drift.
    let budget_ppm = 40.0 / 3600.0 * 1000.0;
    if ppm.abs() <= budget_ppm {
        println!(
            "VERDICT           : within C2 ({:.1} ppm budget) without compensation",
            budget_ppm
        );
    } else {
        println!(
            "VERDICT           : exceeds C2 ({:.1} ppm budget) — compensation required",
            budget_ppm
        );
    }
}

fn report(out: &Path, frames: u64, outcomes: &[Option<ThreadOutcome>]) {
    let size = directory_or_file_size(out);
    let duration = frames as f64 / f64::from(TARGET_SAMPLE_RATE);

    println!("\n--- Capture report ---");
    println!("Stereo frames     : {frames}");
    println!("Duration          : {duration:.2} s");
    println!("File size         : {size} bytes");
    println!("Output            : {}", out.display());

    for outcome in outcomes.iter().flatten() {
        let stats = &outcome.stats;
        let padded_share = if stats.frames == 0 {
            0.0
        } else {
            stats.padded_frames as f64 * 100.0 / stats.frames as f64
        };
        println!("\n[{}]", outcome.endpoint);
        println!("  device          : {}", outcome.device_name);
        println!(
            "  native format   : {} Hz, {} ch -> {} Hz mono",
            outcome.input_rate, outcome.input_channels, TARGET_SAMPLE_RATE
        );
        println!(
            "  captured        : {} frames ({:.2} s at native rate) over {:.2} s of wall clock",
            stats.frames,
            stats.duration(outcome.input_rate).as_secs_f64(),
            outcome.elapsed.as_secs_f64()
        );
        // C3, measured properly. The Windows flag stays on the line below as
        // a hint, which is all it ever deserved to be.
        match outcome.delivery.shortfall_percent(outcome.input_rate) {
            Some(shortfall) => println!(
                "  REAL LOSS (C3)  : {shortfall:.3} % over {:.1} s of streaming ({} frames delivered)",
                outcome.delivery.window_seconds().unwrap_or(0.0),
                outcome.real_frames()
            ),
            // Said plainly rather than printed as 100 %. A loopback endpoint
            // delivers nothing while nothing plays, and calling that a total
            // loss is arithmetic nobody can act on.
            None => println!(
                "  REAL LOSS (C3)  : no measurement - this endpoint never streamed (nothing was playing)"
            ),
        }
        println!(
            "  discontinuities : {}  (Windows' own flag, which under-reports)",
            stats.discontinuities
        );
        println!("  silent packets  : {}", stats.silent_packets);
        println!(
            "  synthesised     : {} frames ({padded_share:.1} %)",
            stats.padded_frames
        );
        println!(
            "  MMCSS Pro Audio : {}",
            if outcome.mmcss { "granted" } else { "DENIED" }
        );
        if stats.device_reopens > 0 {
            println!(
                "  DEVICE REOPENED : {} time(s) - Windows retired the device mid-recording",
                stats.device_reopens
            );
        }
        if outcome.overflow.happened() {
            println!(
                "  QUEUE OVERFLOW  : {} chunks / {} frames dropped - the writer could not keep up",
                outcome.overflow.chunks, outcome.overflow.frames
            );
        }
        match &outcome.drift {
            Ok(drift) => println!(
                "  clock           : {:.3} Hz measured vs {:.0} nominal = {:+.2} ppm                  (over {:.0} s, {} points)",
                drift.measured_rate,
                drift.nominal_rate,
                drift.ppm,
                drift.observed_seconds,
                drift.samples
            ),
            // Printed in full rather than swallowed: the reason is the useful
            // part. "8.3 % of frames were synthesised" tells an operator what
            // to fix; a missing line tells them nothing, and a bogus -83 293
            // ppm tells them something false.
            Err(reason) => println!("  clock           : no measurement - {reason}"),
        }
    }
}

/// Total bytes at `path`, whether it is a single file or a directory of
/// segments.
fn directory_or_file_size(path: &Path) -> u64 {
    let Ok(meta) = std::fs::metadata(path) else {
        return 0;
    };
    if meta.is_file() {
        return meta.len();
    }
    std::fs::read_dir(path)
        .map(|entries| {
            entries
                .filter_map(std::result::Result::ok)
                .filter_map(|entry| entry.metadata().ok())
                .filter(std::fs::Metadata::is_file)
                .map(|m| m.len())
                .sum()
        })
        .unwrap_or(0)
}

/// Draw a VU meter for each channel.
fn render_meters(mic: f32, system: f32, no_tty: bool) {
    let line = format!("mic [{}]  sys [{}]", meter(mic), meter(system));
    if no_tty {
        println!("{line}");
    } else {
        print!("\r{line}");
        let _ = std::io::stdout().flush();
    }
}

fn meter(level: f32) -> String {
    const WIDTH: usize = 24;
    let filled = ((level.clamp(0.0, 1.0) * WIDTH as f32) as usize).min(WIDTH);
    let bar = "#".repeat(filled) + &"-".repeat(WIDTH - filled);
    let db = if level > 0.0 {
        format!("{:6.1}", 20.0 * level.log10())
    } else {
        "  -inf".to_owned()
    };
    format!("{bar} {db} dBFS")
}

/// Install a Ctrl+C handler so a recording stopped by hand still finalises its
/// WAV header instead of leaving a truncated file behind.
#[cfg(windows)]
fn install_ctrlc_handler<F>(on_signal: F) -> Result<()>
where
    F: Fn() + Send + Sync + 'static,
{
    use std::sync::OnceLock;
    use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;

    static HANDLER: OnceLock<Box<dyn Fn() + Send + Sync>> = OnceLock::new();

    // windows-sys models Win32 BOOL as a plain i32.
    unsafe extern "system" fn trampoline(_ctrl_type: u32) -> i32 {
        if let Some(handler) = HANDLER.get() {
            handler();
        }
        1 // handled: do not let the default handler kill the process
    }

    HANDLER
        .set(Box::new(on_signal))
        .map_err(|_| anyhow!("the Ctrl+C handler was installed twice"))?;

    // SAFETY: registering a plain function pointer with the console subsystem.
    let ok = unsafe { SetConsoleCtrlHandler(Some(trampoline), 1) };
    anyhow::ensure!(ok != 0, "could not install the Ctrl+C handler");
    Ok(())
}

#[cfg(not(windows))]
fn install_ctrlc_handler<F>(_on_signal: F) -> Result<()>
where
    F: Fn() + Send + Sync + 'static,
{
    anyhow::bail!("nb-capture only runs on Windows: it captures audio through WASAPI")
}

#[cfg(test)]
mod tests {
    use super::{BoundedSink, Offered};

    use super::silence_needed;

    /// D5, found by the 60-minute validation capture rather than by review.
    ///
    /// The loopback endpoint stopped at minute 28. Nothing padded the right
    /// channel, the mixer held every microphone frame waiting for a
    /// counterpart that never came, and the backlog grew one second per second:
    /// 6 003 681 frames and 34.8 MB by minute 35, heading past a gigabyte over
    /// the four hours EF-34 allows.
    #[test]
    fn a_side_that_stops_is_padded_rather_than_waited_for() {
        // The microphone leads by ten seconds at 16 kHz and the system side is
        // gone: the whole lead has to be covered with silence.
        let (left, right) = silence_needed(160_000, true, false);
        assert_eq!(left, 0);
        assert_eq!(right, 160_000);
    }

    #[test]
    fn the_same_holds_when_it_is_the_microphone_that_stops() {
        let (left, right) = silence_needed(-160_000, false, true);
        assert_eq!(left, 160_000);
        assert_eq!(right, 0);
    }

    /// While both are running the mixer holds the surplus on purpose - that is
    /// what keeps the two voices aligned, and the backlog is bounded by the
    /// difference between two device clocks, which is milliseconds.
    #[test]
    fn a_healthy_lead_is_left_alone() {
        assert_eq!(silence_needed(4_000, true, true), (0, 0));
        assert_eq!(silence_needed(-4_000, true, true), (0, 0));
    }

    /// An endpoint nobody asked to record: the original condition, still
    /// covered.
    #[test]
    fn an_endpoint_that_was_never_recorded_is_padded_too() {
        assert_eq!(silence_needed(48_000, true, false).1, 48_000);
        assert_eq!(silence_needed(48_000, false, false), (0, 48_000));
    }

    /// Nothing to pad when the side that stopped is the one already behind.
    #[test]
    fn a_stopped_side_that_is_ahead_needs_nothing() {
        assert_eq!(silence_needed(-5_000, true, false), (0, 0));
        assert_eq!(silence_needed(0, true, false), (0, 0));
    }

    use super::Delivery;

    /// 100 ms of packets at exactly 48 kHz: nothing missing.
    fn stream(delivery: &mut Delivery, rate: f64, seconds: f64, packet_ms: f64, share: f64) {
        let packets = (seconds * 1000.0 / packet_ms) as u64;
        let mut frames = 0_u64;
        for packet in 0..packets {
            let elapsed = packet as f64 * packet_ms / 1000.0;
            let qpc = 5_000_000_000 + (elapsed * 10_000_000.0) as u64;
            delivery.observe(qpc, frames);
            // `share` of the packets arrive; the rest are the ones that went
            // missing, so the frame counter advances by less than the clock.
            frames += (rate * packet_ms / 1000.0 * share) as u64;
        }
    }

    #[test]
    fn a_clean_stream_reports_no_loss() {
        let mut delivery = Delivery::default();
        stream(&mut delivery, 48_000.0, 60.0, 20.0, 1.0);

        let shortfall = delivery
            .shortfall_percent(48_000)
            .expect("a minute of packets is measurable");
        assert!(shortfall < 0.1, "expected no loss, got {shortfall} %");
    }

    /// The 2026-09-07 measurement, as a test: a device delivering 89.75 % of
    /// what its own clock owes must be reported as having lost 10.25 %.
    #[test]
    fn a_stream_missing_a_tenth_reports_a_tenth() {
        let mut delivery = Delivery::default();
        stream(&mut delivery, 48_000.0, 3600.0, 20.0, 0.8975);

        let shortfall = delivery
            .shortfall_percent(48_000)
            .expect("an hour of packets is measurable");
        assert!(
            (shortfall - 10.25).abs() < 0.2,
            "expected about 10.25 % lost, got {shortfall} %"
        );
    }

    /// A loopback endpoint with nothing playing delivers no packets at all.
    /// Against the wall clock that reads as 100 % loss, which is true
    /// arithmetic and a useless statement; the answer is that there is no
    /// measurement to make.
    #[test]
    fn an_endpoint_that_never_streamed_has_no_measurement() {
        let delivery = Delivery::default();
        assert_eq!(delivery.shortfall_percent(48_000), None);
    }

    /// One packet is an instant, not a window. Dividing by it would produce a
    /// number out of nothing.
    #[test]
    fn a_single_packet_is_not_a_window() {
        let mut delivery = Delivery::default();
        delivery.observe(5_000_000_000, 0);
        assert_eq!(delivery.shortfall_percent(48_000), None);
    }

    /// Device startup must not be charged as loss. The window opens at the
    /// first delivered packet, so a device that took 200 ms to come alive still
    /// measures clean - which is the difference between 0.000 % and the
    /// 0.225 % a wall-clock denominator reported on a flawless capture.
    #[test]
    fn a_slow_device_start_is_not_counted_as_loss() {
        let mut delivery = Delivery::default();
        // The first packet lands a full second after the capture began; the
        // window starts there and ignores everything before it.
        stream(&mut delivery, 48_000.0, 30.0, 20.0, 1.0);

        let shortfall = delivery.shortfall_percent(48_000).expect("measurable");
        assert!(
            shortfall < 0.1,
            "startup latency leaked into the measurement: {shortfall} %"
        );
    }

    use std::sync::mpsc::sync_channel;

    /// Criterion 4 of the brief: a producer faster than its consumer must leave
    /// memory bounded and say that it did.
    ///
    /// The unbounded queues are the other half of the hour-long failure. When
    /// the writer fell behind nothing pushed back, so the backlog grew, the
    /// quadratic drain made the writer slower still, and 668 MB later the
    /// capture threads were missing their own deadlines. Nothing reported any
    /// of it: the recording simply came out short.
    #[test]
    fn a_producer_faster_than_its_consumer_is_bounded_and_counted() {
        const DEPTH: usize = 8;
        const OFFERED: usize = 1_000;
        const FRAMES_EACH: u64 = 160;

        let (tx, rx) = sync_channel::<u32>(DEPTH);
        let mut sink = BoundedSink::new(tx);

        // Nobody ever receives, which is the worst case the writer can present.
        let mut taken = 0_usize;
        for item in 0..OFFERED {
            match sink.offer(item as u32, FRAMES_EACH) {
                Offered::Taken => taken += 1,
                Offered::Dropped { .. } => {}
                Offered::Closed => panic!("the receiver is still alive"),
            }
        }

        // Bounded: the queue holds the depth it was given, and not one more,
        // however long the producer runs.
        assert_eq!(taken, DEPTH, "the queue accepted more than its depth");
        assert_eq!(
            rx.try_iter().count(),
            DEPTH,
            "the queue held more than its depth"
        );

        // Counted: and in frames of audio, not in messages, because that is
        // what the person reading the report needs to know.
        assert_eq!(sink.overflow.chunks as usize, OFFERED - DEPTH);
        assert_eq!(
            sink.overflow.frames,
            (OFFERED - DEPTH) as u64 * FRAMES_EACH,
            "the counter must measure lost audio, not lost messages"
        );
    }

    /// The warning is worth printing once. A hundred times a second it becomes
    /// noise that hides the line above it.
    #[test]
    fn only_the_first_drop_announces_itself() {
        let (tx, _rx) = sync_channel::<u32>(1);
        let mut sink = BoundedSink::new(tx);

        assert_eq!(sink.offer(0, 1), Offered::Taken);
        assert_eq!(sink.offer(1, 1), Offered::Dropped { first: true });
        for _ in 0..10 {
            assert_eq!(sink.offer(2, 1), Offered::Dropped { first: false });
        }
    }

    /// A writer that has gone away is not a saturation, and must not be counted
    /// as lost audio: there is simply nothing left to send to.
    #[test]
    fn a_closed_writer_is_not_counted_as_loss() {
        let (tx, rx) = sync_channel::<u32>(4);
        let mut sink = BoundedSink::new(tx);
        drop(rx);

        assert_eq!(sink.offer(0, 160), Offered::Closed);
        assert_eq!(sink.overflow.chunks, 0);
        assert_eq!(sink.overflow.frames, 0);
    }
}
