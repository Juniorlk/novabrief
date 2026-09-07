//! `nb-capture` — command-line recorder used to validate POC #1.
//!
//! Milestone reached: loopback only, written to a WAV file, which is the first
//! of the three gates Novafrik set (loopback alone -> valid WAV, then
//! microphone + loopback, then 60 minutes continuous).
//!
//! Dependencies are deliberately minimal — no `clap`, no `anyhow`. Smart App
//! Control is enforcing on the development machine and blocks Cargo build
//! scripts and proc-macro DLLs, so this binary sticks to crates that need
//! neither. See the task report for the decision that has to be made about it.
//!
//! Printing to stdout is the point of this binary: it shows a live text VU
//! meter while recording, so a human can see signal arriving.
#![allow(clippy::print_stdout)]

use std::error::Error;
use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use audio_engine::{peak, LoopbackCapture};

type Failure = Box<dyn Error>;

/// How often the VU meter is redrawn.
const METER_REFRESH: Duration = Duration::from_millis(100);

const USAGE: &str = "\
nb-capture — capture Windows system audio (WASAPI loopback) to a WAV file

USAGE:
    nb-capture [OPTIONS]

OPTIONS:
    -d, --duration <SECONDS>   Recording length. Omit to record until Ctrl+C.
    -o, --out <FILE>           Output file (default: capture.wav)
        --no-tty               Print one meter line per refresh instead of
                               redrawing in place, for piping to a file.
    -h, --help                 Show this help.
";

#[derive(Debug)]
struct Options {
    duration: Option<Duration>,
    out: PathBuf,
    no_tty: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            duration: None,
            out: PathBuf::from("capture.wav"),
            no_tty: false,
        }
    }
}

/// Parse the command line. Returns `Ok(None)` when help was requested.
fn parse_args() -> Result<Option<Options>, Failure> {
    let mut options = Options::default();
    let mut args = std::env::args().skip(1);

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => return Ok(None),
            "-d" | "--duration" => {
                let value = args
                    .next()
                    .ok_or("--duration expects a number of seconds")?;
                let seconds: u64 = value.parse().map_err(|_| {
                    format!("--duration expects a number of seconds, got {value:?}")
                })?;
                options.duration = Some(Duration::from_secs(seconds));
            }
            "-o" | "--out" => {
                let value = args.next().ok_or("--out expects a file path")?;
                options.out = PathBuf::from(value);
            }
            "--no-tty" => options.no_tty = true,
            other => return Err(format!("unknown argument {other:?}\n\n{USAGE}").into()),
        }
    }

    Ok(Some(options))
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("nb-capture: {error}");
            let mut source = error.source();
            while let Some(cause) = source {
                eprintln!("  caused by: {cause}");
                source = cause.source();
            }
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Failure> {
    let Some(options) = parse_args()? else {
        print!("{USAGE}");
        return Ok(());
    };

    let mut capture = LoopbackCapture::open().map_err(|error| {
        format!(
            "{error}\nCheck that an output device is present and enabled in \
             Windows sound settings."
        )
    })?;

    let format = capture.format();
    println!(
        "Device format : {} Hz, {} ch, {:?}",
        format.sample_rate, format.channels, format.sample_format
    );
    println!("Output        : {}", options.out.display());
    match options.duration {
        Some(duration) => println!("Duration      : {} s", duration.as_secs()),
        None => println!("Duration      : until Ctrl+C"),
    }

    // The WAV mirrors the endpoint format exactly: this milestone proves the
    // capture, so it must not hide anything behind a conversion.
    let spec = hound::WavSpec {
        channels: format.channels,
        sample_rate: format.sample_rate,
        bits_per_sample: 32,
        sample_format: hound::SampleFormat::Float,
    };
    let mut writer = hound::WavWriter::create(&options.out, spec)
        .map_err(|error| format!("could not create {}: {error}", options.out.display()))?;

    let stop = Arc::new(AtomicBool::new(false));
    let ctrlc_flag = Arc::clone(&stop);
    install_ctrlc_handler(move || ctrlc_flag.store(true, Ordering::Relaxed))?;

    println!("\nRecording. Press Ctrl+C to stop.\n");

    let mut last_meter = Instant::now();
    let mut window_peak = 0.0_f32;
    let mut write_error: Option<hound::Error> = None;

    let stats = capture.record(&stop, options.duration, |samples| {
        if write_error.is_some() {
            return;
        }
        for &sample in samples {
            if let Err(error) = writer.write_sample(sample) {
                write_error = Some(error);
                return;
            }
        }

        window_peak = window_peak.max(peak(samples));
        if last_meter.elapsed() >= METER_REFRESH {
            render_meter(window_peak, options.no_tty);
            window_peak = 0.0;
            last_meter = Instant::now();
        }
    })?;

    if let Some(error) = write_error {
        return Err(format!("writing the WAV file failed mid-capture: {error}").into());
    }
    writer
        .finalize()
        .map_err(|error| format!("could not finalize the WAV file: {error}"))?;

    report(
        &options.out,
        &stats,
        format.sample_rate,
        capture.mmcss_active(),
    );
    Ok(())
}

fn report(
    out: &std::path::Path,
    stats: &audio_engine::CaptureStats,
    sample_rate: u32,
    mmcss: bool,
) {
    let size = std::fs::metadata(out).map(|meta| meta.len()).unwrap_or(0);
    let padded_share = if stats.frames == 0 {
        0.0
    } else {
        stats.padded_frames as f64 * 100.0 / stats.frames as f64
    };

    println!("\n\n--- Capture report ---");
    println!(
        "Duration          : {:.2} s",
        stats.duration(sample_rate).as_secs_f64()
    );
    println!("Frames            : {}", stats.frames);
    println!(
        "Discontinuities   : {}  (C3 target: 0)",
        stats.discontinuities
    );
    println!("Silent packets    : {}", stats.silent_packets);
    println!(
        "Synthesised frames: {} ({padded_share:.1} % of the file)",
        stats.padded_frames
    );
    println!(
        "MMCSS Pro Audio   : {}",
        if mmcss { "granted" } else { "DENIED" }
    );
    println!("File size         : {size} bytes");
    println!("Output            : {}", out.display());
}

/// Draw a 40-character VU meter for the captured signal.
fn render_meter(level: f32, no_tty: bool) {
    const WIDTH: usize = 40;
    let filled = ((level.clamp(0.0, 1.0) * WIDTH as f32) as usize).min(WIDTH);
    let bar: String = "#".repeat(filled) + &"-".repeat(WIDTH - filled);
    let db = if level > 0.0 {
        format!("{:6.1} dBFS", 20.0 * level.log10())
    } else {
        "  -inf dBFS".to_owned()
    };

    if no_tty {
        println!("system [{bar}] {db}");
    } else {
        print!("\rsystem [{bar}] {db}");
        let _ = std::io::stdout().flush();
    }
}

/// Install a Ctrl+C handler so a recording stopped by hand still finalises its
/// WAV header instead of leaving a truncated file behind.
#[cfg(windows)]
fn install_ctrlc_handler<F>(on_signal: F) -> Result<(), Failure>
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
        .map_err(|_| "the Ctrl+C handler was installed twice")?;

    // SAFETY: registering a plain function pointer with the console subsystem.
    let ok = unsafe { SetConsoleCtrlHandler(Some(trampoline), 1) };
    if ok == 0 {
        return Err("could not install the Ctrl+C handler".into());
    }
    Ok(())
}

#[cfg(not(windows))]
fn install_ctrlc_handler<F>(_on_signal: F) -> Result<(), Failure>
where
    F: Fn() + Send + Sync + 'static,
{
    Err("nb-capture only runs on Windows: it captures audio through WASAPI".into())
}
