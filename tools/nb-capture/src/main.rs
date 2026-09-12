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
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use audio_engine::{
    projected_offset_ms, relative_ppm, DriftEstimate, Endpoint, EndpointOutcome, Endpoints,
    Recorder, SegmentedOpusWriter, TARGET_SAMPLE_RATE,
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

/// Frames the device really delivered, synthesised silence excluded.
const fn real_frames(outcome: &EndpointOutcome) -> u64 {
    outcome
        .stats
        .frames
        .saturating_sub(outcome.stats.padded_frames)
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
    /// Take the packet bounds the pipeline recorded.
    const fn from_outcome(outcome: &EndpointOutcome) -> Self {
        Self::from_bounds(outcome.first_packet, outcome.last_packet)
    }

    /// The two events the window is measured between.
    const fn from_bounds(first: Option<(u64, u64)>, last: Option<(u64, u64)>) -> Self {
        Self { first, last }
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

fn main() -> Result<()> {
    let cli = Cli::parse();

    if cli.list_devices {
        return list_all_devices();
    }

    let endpoints = Endpoints {
        microphone: cli.input.clone(),
        system: cli.output.clone(),
        without_microphone: cli.system_only,
        without_system: cli.mic_only,
    };
    let max_duration = cli.duration.map(Duration::from_secs);

    let recorder =
        Recorder::start(&endpoints, max_duration).context("could not open the audio endpoints")?;
    let stop = Arc::new(AtomicBool::new(false));
    let ctrlc_flag = Arc::clone(&stop);
    install_ctrlc_handler(move || ctrlc_flag.store(true, Ordering::Relaxed))?;

    println!("Working rate  : {TARGET_SAMPLE_RATE} Hz, stereo (L = microphone, R = system)");
    println!("Output        : {}", cli.out.display());
    match max_duration {
        Some(duration) => println!("Duration      : {} s", duration.as_secs()),
        None => println!("Duration      : until Ctrl+C"),
    }
    println!("\nRecording. Press Ctrl+C to stop.\n");

    let (written, sink, skew_frames, outcomes) = write_stereo(&cli, recorder, &stop)?;

    let device_of = |endpoint: Endpoint| {
        outcomes
            .iter()
            .find(|outcome| outcome.endpoint == endpoint)
            .map_or_else(|| "not recorded".to_owned(), |o| o.device_name.clone())
    };
    let skew_ms = skew_frames.unwrap_or(0) * 1000 / i64::from(TARGET_SAMPLE_RATE);
    let output_path = match cli.format {
        Format::Wav => cli.out.clone(),
        Format::Opus => opus_directory(&cli.out),
    };
    finalize(
        sink,
        &device_of(Endpoint::Microphone),
        &device_of(Endpoint::SystemLoopback),
        skew_ms,
    )?;

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

/// Interleave both streams into a stereo WAV until the capture threads stop.
///
/// Returns the number of stereo frames written.
fn write_stereo(
    cli: &Cli,
    mut recorder: Recorder,
    stop: &AtomicBool,
) -> Result<(u64, Sink, Option<i64>, Vec<EndpointOutcome>)> {
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

    let mut interleaved = Vec::new();
    let mut frames: u64 = 0;
    let mut last_meter = Instant::now();
    let mut last_memory = Instant::now();
    let started = Instant::now();
    let mut peak_resident: u64 = 0;

    while recorder.running() && !stop.load(Ordering::Relaxed) {
        interleaved.clear();
        let produced = recorder.poll(&mut interleaved);
        sink.write(&interleaved)
            .context("writing the capture failed mid-recording")?;
        frames += produced as u64;

        for endpoint in recorder.newly_stalled() {
            eprintln!("\n!! {endpoint} stopped delivering; the rest is silence on that channel");
        }

        if last_meter.elapsed() >= METER_REFRESH {
            let (mic, system) = recorder.levels();
            render_meters(mic, system, cli.no_tty);
            last_meter = Instant::now();
        }

        // Criterion 2 of the correction brief: the slope matters more than the
        // endpoint, so this prints as the capture runs rather than once at the
        // end. The 2026-09-07 run reached 668 MB at 32 minutes; readings at 5,
        // 10 and 15 would have shown the slope long before that.
        if last_memory.elapsed() >= MEMORY_REPORT_INTERVAL {
            if let Some(bytes) = resident_bytes() {
                peak_resident = peak_resident.max(bytes);
                println!(
                    "\n[{:>5.1} min] resident memory: {:.1} MB",
                    started.elapsed().as_secs_f64() / 60.0,
                    bytes as f64 / 1_048_576.0,
                );
            }
            last_memory = Instant::now();
        }

        if produced == 0 {
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    let skew = recorder.applied_skew();
    let worst_imbalance = recorder.worst_imbalance();

    interleaved.clear();
    let outcomes = recorder.finish(&mut interleaved)?;
    sink.write(&interleaved)
        .context("writing the capture tail failed")?;
    frames += (interleaved.len() / 2) as u64;

    let to_ms = |f: i64| f as f64 * 1000.0 / f64::from(TARGET_SAMPLE_RATE);
    println!();
    match skew {
        Some(frames) => println!(
            "\nStart skew (measured from the device clock): {frames} frames ({:.1} ms), corrected",
            to_ms(frames)
        ),
        // Not the same as a skew of zero. One endpoint never delivered a real
        // packet - which happens whenever nothing is playing - so there was
        // nothing to line the other one up against. Reporting that as "no skew"
        // would claim the two streams are aligned when nobody checked.
        None => {
            println!("\nStart skew: not measurable (one endpoint never delivered a real packet)")
        }
    }
    println!(
        "Worst divergence while both streams ran   : {worst_imbalance} frames ({:.1} ms)",
        to_ms(worst_imbalance)
    );

    let final_resident = resident_bytes().unwrap_or(0);
    peak_resident = peak_resident.max(final_resident);
    if peak_resident > 0 {
        println!(
            "Peak resident memory (C2 budget 50 MB)    : {:.1} MB",
            peak_resident as f64 / 1_048_576.0
        );
    }

    Ok((frames, sink, skew, outcomes))
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
fn report_relative_drift(outcomes: &[EndpointOutcome]) {
    let drifts: Vec<DriftEstimate> = outcomes
        .iter()
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
        for outcome in outcomes {
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

fn report(out: &Path, frames: u64, outcomes: &[EndpointOutcome]) {
    let size = directory_or_file_size(out);
    let duration = frames as f64 / f64::from(TARGET_SAMPLE_RATE);

    println!("\n--- Capture report ---");
    println!("Stereo frames     : {frames}");
    println!("Duration          : {duration:.2} s");
    println!("File size         : {size} bytes");
    println!("Output            : {}", out.display());

    for outcome in outcomes {
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
        match Delivery::from_outcome(outcome).shortfall_percent(outcome.input_rate) {
            Some(shortfall) => println!(
                "  REAL LOSS (C3)  : {shortfall:.3} % over {:.1} s of streaming ({} frames delivered)",
                Delivery::from_outcome(outcome).window_seconds().unwrap_or(0.0),
                real_frames(outcome)
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
    use super::Delivery;

    /// A device that ran for `seconds` and delivered `share` of what its own
    /// clock says it owed. The window is the two packet bounds the pipeline
    /// reports, which is what the measurement is actually built on.
    fn stream(rate: f64, seconds: f64, share: f64) -> Delivery {
        let delivered = (rate * seconds * share) as u64;
        Delivery::from_bounds(
            Some((5_000_000_000, 0)),
            Some((5_000_000_000 + (seconds * 10_000_000.0) as u64, delivered)),
        )
    }

    #[test]
    fn a_clean_stream_reports_no_loss() {
        let shortfall = stream(48_000.0, 60.0, 1.0)
            .shortfall_percent(48_000)
            .expect("a minute of packets is measurable");
        assert!(shortfall < 0.1, "expected no loss, got {shortfall} %");
    }

    /// The 2026-09-07 measurement, as a test: a device delivering 89.75 % of
    /// what its own clock owes must be reported as having lost 10.25 %.
    #[test]
    fn a_stream_missing_a_tenth_reports_a_tenth() {
        let shortfall = stream(48_000.0, 3600.0, 0.8975)
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
        assert_eq!(Delivery::default().shortfall_percent(48_000), None);
    }

    /// One packet is an instant, not a window. Dividing by it would produce a
    /// number out of nothing.
    #[test]
    fn a_single_packet_is_not_a_window() {
        let delivery = Delivery::from_bounds(Some((5_000_000_000, 0)), Some((5_000_000_000, 0)));
        assert_eq!(delivery.shortfall_percent(48_000), None);
    }

    /// Device startup must not be charged as loss. The window opens at the
    /// first delivered packet, so a device that took a second to come alive
    /// still measures clean - the difference between 0.000 % and the 0.225 % a
    /// wall-clock denominator reported on a flawless capture.
    #[test]
    fn a_slow_device_start_is_not_counted_as_loss() {
        // The first packet lands a full second after the capture began.
        let delivery =
            Delivery::from_bounds(Some((5_010_000_000, 0)), Some((5_310_000_000, 1_440_000)));
        let shortfall = delivery.shortfall_percent(48_000).expect("measurable");
        assert!(
            shortfall < 0.1,
            "startup latency leaked into the measurement: {shortfall} %"
        );
    }
}
