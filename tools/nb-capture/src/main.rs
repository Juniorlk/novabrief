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
use std::sync::mpsc::{channel, Receiver, Sender, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use audio_engine::{
    downmix_to_mono, peak, CaptureStats, Endpoint, EndpointCapture, MonoResampler,
    SegmentedOpusWriter, StereoMixer, TARGET_SAMPLE_RATE,
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
    let (mic_tx, mic_rx) = channel::<Chunk>();
    let (sys_tx, sys_rx) = channel::<Chunk>();

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

    report(&output_path, written, &[mic_outcome, sys_outcome]);
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
    tx: Sender<Chunk>,
) -> std::thread::JoinHandle<Result<ThreadOutcome>> {
    std::thread::spawn(move || -> Result<ThreadOutcome> {
        let mut capture = EndpointCapture::open_named(endpoint, device.as_deref())
            .with_context(|| format!("could not open the {endpoint} endpoint"))?;

        let format = capture.format();
        let mut resampler = MonoResampler::new(format.sample_rate);
        let mut mono = Vec::new();
        let mut converted = Vec::new();
        let mut first_qpc: Option<u64> = None;

        let stats = capture.record(&stop, max_duration, |packet| {
            // Use the engine's reconstructed stream start, not the raw packet
            // timestamp: for a loopback endpoint the first packet only arrives
            // once something plays, which can be far into the recording.
            if first_qpc.is_none() {
                first_qpc = packet.stream_start_qpc_100ns;
            }
            downmix_to_mono(packet.samples, format.channels, &mut mono);
            converted.clear();
            resampler.process(&mono, &mut converted);
            if !converted.is_empty() {
                // A closed receiver means the writer stopped; the capture then
                // has nowhere to send audio, so there is nothing to do but
                // let the loop wind down on the stop flag.
                let _ = tx.send(Chunk {
                    samples: std::mem::take(&mut converted),
                    first_qpc_100ns: first_qpc,
                });
            }
        })?;

        Ok(ThreadOutcome {
            endpoint,
            device_name: capture.device_name().to_owned(),
            input_rate: format.sample_rate,
            input_channels: format.channels,
            mmcss: capture.mmcss_active(),
            stats,
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

        // A side that is not being recorded never produces samples, so it is fed
        // silence to match the side that is: without it no stereo frame would
        // ever be complete and the file would stay empty.
        if !mic_recorded {
            let deficit = usize::try_from(-mixer.imbalance()).unwrap_or(0);
            if deficit > 0 {
                mixer.push_left(&vec![0.0; deficit]);
            }
        }
        if !sys_recorded {
            let deficit = usize::try_from(mixer.imbalance()).unwrap_or(0);
            if deficit > 0 {
                mixer.push_right(&vec![0.0; deficit]);
            }
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
    Ok((frames, sink, applied_skew.unwrap_or(0)))
}

/// Directory that holds the Opus segments and their manifest.
///
/// `--out meeting.opus` and `--out meeting` both write into `meeting/`, so the
/// segments never collide with an unrelated file.
fn opus_directory(out: &Path) -> PathBuf {
    out.with_extension("")
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
            "  captured        : {} frames ({:.2} s at native rate)",
            stats.frames,
            stats.duration(outcome.input_rate).as_secs_f64()
        );
        println!(
            "  discontinuities : {}  (C3 target: 0)",
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
