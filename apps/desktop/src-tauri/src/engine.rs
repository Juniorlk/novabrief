//! The loop that turns two open audio devices into encrypted segments.
//!
//! Everything above this module is rules (`session.rs`) or bytes
//! (`recording.rs`); everything below it is hardware
//! (`audio_engine::pipeline`). This is the only place the two meet.
//!
//! It is a thread rather than a set of callbacks on the UI, and that is the
//! point: the capture queues hold three seconds of audio and have to be
//! drained on a schedule a WebView cannot be trusted to keep. A window that
//! stops polling while Windows paints a dialog would otherwise cost audio, and
//! the failure would look like a supplier problem rather than ours.
//!
//! The loop is driven through [`Source`], so pause, budget, tail and vault can
//! all be tested end to end against a generated tone, with no microphone.

use std::sync::mpsc::{channel, Receiver, Sender, TryRecvError};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use audio_engine::encode::{Manifest as AudioManifest, SegmentedOpusWriter};
use audio_engine::resample::TARGET_SAMPLE_RATE;
use serde::Serialize;
use vault::{DpapiSealer, RecordingState, Vault};

use crate::recording::VaultSegmentSink;
use crate::session::{BudgetEvent, Disposition, Session};
use crate::state::AppState;

/// How long the loop sleeps when the devices had nothing to give.
///
/// A fortieth of one capture chunk, so the queues never approach their depth,
/// and short enough that a Pause is acted on long before anybody perceives it.
const IDLE_POLL: Duration = Duration::from_millis(5);

/// How much of the recent past one reading of the meters covers.
///
/// The meters exist for EF-13 - "a flat system meter is noticed in under five
/// seconds" - so they show the loudest moment of a tenth of a second rather
/// than an average. Speech averaged over a second sits near silence, which is
/// the very reading the criterion has to distinguish a dead channel from.
const METER_WINDOW: Duration = Duration::from_millis(100);

/// How long a command waits for the loop to answer.
///
/// The loop answers within one iteration, so the only way to reach this is a
/// thread that has died. Waiting forever would hang the tray menu with it.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(5);

/// What went wrong with a recording.
#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    /// Encoding or closing the stream failed.
    #[error("{0}")]
    Encode(#[from] audio_engine::EncodeError),

    /// The encrypted store refused.
    #[error("{0}")]
    Vault(#[from] vault::VaultError),

    /// The recording thread died without saying why.
    #[error("the recording stopped unexpectedly")]
    Panicked,
}

/// Where the stereo frames come from.
///
/// Implemented once for real devices and once, in the tests, for a generated
/// tone. Without this seam the only way to check that a pause keeps nothing
/// would be to record a real meeting and look at what landed on the disk.
pub trait Source: Send {
    /// Append whatever is ready, interleaved, left = microphone. Returns the
    /// number of stereo frames appended.
    fn poll(&mut self, out: &mut Vec<f32>) -> usize;

    /// Peak on each channel since the previous call, `(microphone, system)`.
    fn levels(&mut self) -> (f32, f32);

    /// Whether either endpoint is still delivering.
    fn running(&self) -> bool;

    /// Endpoints that stopped delivering since the previous call.
    fn newly_stalled(&mut self) -> Vec<String>;

    /// Stop the devices, append the tail, and say what was recorded with.
    ///
    /// # Errors
    ///
    /// A message for the journal if an endpoint failed. Frames already
    /// appended are still valid audio, so a failure here never means the
    /// meeting is lost.
    fn finish(&mut self, out: &mut Vec<f32>) -> Result<Report, String>;
}

/// What the capture says about itself once it is over.
#[derive(Debug, Clone)]
pub struct Report {
    /// The microphone that was actually opened.
    pub input_device: String,
    /// The render device that was actually captured in loopback.
    pub output_device: String,
    /// The skew between the two device clocks that was corrected at the start.
    pub start_skew_ms: i64,
}

impl Default for Report {
    fn default() -> Self {
        Self {
            input_device: "unknown".to_owned(),
            output_device: "unknown".to_owned(),
            start_skew_ms: 0,
        }
    }
}

/// What a recording in progress looks like from outside.
///
/// One value, read whole. The alternative - the tray asking for the state, the
/// widget for the timer, the meters for their levels - lets a window draw
/// "paused" beside a running clock, and the two would be right about different
/// instants.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    /// Where the recording is.
    pub state: AppState,
    /// The identifier the vault directory is named after.
    pub meeting_id: String,
    /// ADR-07, from the API when it was reachable at the start.
    pub debug_id: String,
    /// Audio kept, which is what gets billed (EF-33).
    pub recorded_ms: u64,
    /// Audio thrown away because the recording was paused.
    pub discarded_ms: u64,
    /// Microphone peak over the last tenth of a second, 0.0 to 1.0.
    pub microphone_level: f32,
    /// System-audio peak over the same window.
    pub system_level: f32,
    /// Whether the five-minute warning of EF-34 has been raised.
    pub warned: bool,
    /// Endpoints that have stopped delivering, by name.
    ///
    /// A meeting recorded with one empty channel is the most expensive failure
    /// this product has, so it is said while it is still happening.
    pub stalled: Vec<String>,
    /// Set if something went wrong; the recording is kept regardless.
    pub failure: Option<String>,
}

impl Snapshot {
    fn new(meeting_id: String, debug_id: String) -> Self {
        Self {
            state: AppState::Recording,
            meeting_id,
            debug_id,
            recorded_ms: 0,
            discarded_ms: 0,
            microphone_level: 0.0,
            system_level: 0.0,
            warned: false,
            stalled: Vec::new(),
            failure: None,
        }
    }
}

/// What a finished recording leaves behind.
#[derive(Debug)]
pub struct Outcome {
    /// The vault directory it is in.
    pub meeting_id: String,
    /// ADR-07.
    pub debug_id: String,
    /// What the encoder produced: segments, digests, durations.
    pub manifest: AudioManifest,
    /// Audio kept.
    pub recorded: Duration,
    /// Audio dropped by pauses.
    pub discarded: Duration,
    /// What went wrong along the way, if anything. The recording is complete
    /// up to that point and is still owed an upload.
    pub failure: Option<String>,
}

/// What the user interface may ask of a running recording.
///
/// Named operations rather than one "move to state X": a generic transition is
/// an interface that lets a UI bug declare a meeting uploaded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Command {
    Pause,
    Resume,
    Finish,
}

struct Request {
    command: Command,
    reply: Sender<Result<AppState, String>>,
}

/// A recording thread, and the two ways to talk to it.
#[derive(Debug)]
pub struct Engine {
    requests: Sender<Request>,
    shared: Arc<Mutex<Snapshot>>,
    thread: Option<JoinHandle<Result<Outcome, EngineError>>>,
}

/// Everything one recording thread owns.
struct Task {
    source: Box<dyn Source>,
    writer: SegmentedOpusWriter<VaultSegmentSink>,
    session: Session,
    vault: Vault<DpapiSealer>,
    meeting_id: String,
    debug_id: String,
    requests: Receiver<Request>,
    shared: Arc<Mutex<Snapshot>>,
}

impl Engine {
    /// Start recording.
    ///
    /// The vault recording is opened by the caller and handed in inside
    /// `writer`, so a store that refuses is reported before anybody is told a
    /// meeting is being recorded.
    #[must_use]
    pub fn start(
        source: Box<dyn Source>,
        writer: SegmentedOpusWriter<VaultSegmentSink>,
        session: Session,
        vault: Vault<DpapiSealer>,
        meeting_id: String,
        debug_id: String,
    ) -> Self {
        let shared = Arc::new(Mutex::new(Snapshot::new(
            meeting_id.clone(),
            debug_id.clone(),
        )));
        let (sender, requests) = channel::<Request>();
        let task = Task {
            source,
            writer,
            session,
            vault,
            meeting_id,
            debug_id,
            requests,
            shared: Arc::clone(&shared),
        };
        let thread = std::thread::Builder::new()
            .name("novabrief-recorder".to_owned())
            .spawn(move || run(task))
            .ok();

        Self {
            requests: sender,
            shared,
            thread,
        }
    }

    /// What the recording looks like right now.
    ///
    /// # Panics
    ///
    /// If the recording thread panicked while holding the lock. Nothing it
    /// does under the lock can panic, so a poisoned lock would mean the
    /// process is already unsound.
    #[must_use]
    pub fn snapshot(&self) -> Snapshot {
        self.shared
            .lock()
            .expect("the snapshot lock is poisoned")
            .clone()
    }

    /// Whether the thread is still running.
    #[must_use]
    pub fn is_running(&self) -> bool {
        self.thread
            .as_ref()
            .is_some_and(|thread| !thread.is_finished())
    }

    /// Suspend capture.
    ///
    /// # Errors
    ///
    /// The loop's own refusal, or a message saying the recording has ended.
    pub fn pause(&self) -> Result<AppState, String> {
        self.ask(Command::Pause)
    }

    /// Carry on.
    ///
    /// # Errors
    ///
    /// See [`Engine::pause`].
    pub fn resume(&self) -> Result<AppState, String> {
        self.ask(Command::Resume)
    }

    /// Stop, flush the tail, and close the vault recording.
    ///
    /// Answers as soon as the move is accepted rather than once the devices
    /// have closed: stopping drains up to a second of queued audio, and a
    /// button that stays stuck down for a second reads as a crash.
    ///
    /// # Errors
    ///
    /// See [`Engine::pause`].
    pub fn stop(&self) -> Result<AppState, String> {
        self.ask(Command::Finish)
    }

    /// Wait for the thread and take what the recording produced.
    ///
    /// # Errors
    ///
    /// [`EngineError`] if the recording could not be closed. Even then the
    /// segments already in the vault are intact.
    pub fn join(self) -> Result<Outcome, EngineError> {
        let Self {
            requests, thread, ..
        } = self;
        // Dropping the sender is what tells a loop nobody has stopped that it
        // should: a recorder whose handle is gone has no one left to stop it.
        drop(requests);
        let Some(thread) = thread else {
            return Err(EngineError::Panicked);
        };
        thread.join().map_err(|_| EngineError::Panicked)?
    }

    fn ask(&self, command: Command) -> Result<AppState, String> {
        let (reply, answer) = channel();
        self.requests
            .send(Request { command, reply })
            .map_err(|_| "the recording has already ended".to_owned())?;
        answer
            .recv_timeout(COMMAND_TIMEOUT)
            .map_err(|_| "the recording has already ended".to_owned())?
    }
}

/// Milliseconds of audio in `frames`, at the working rate.
const fn frames_to_ms(frames: u64) -> u64 {
    frames * 1000 / TARGET_SAMPLE_RATE as u64
}

/// Add `frames` to a running total and return the whole milliseconds that adds.
///
/// Converting each poll's own frame count would round it away: at 5 ms per
/// wake-up most polls carry a fraction of a millisecond, and a recording
/// billed from the sum of those would come out minutes short over an hour.
fn advance(total: &mut u64, frames: usize) -> u64 {
    let before = frames_to_ms(*total);
    *total += frames as u64;
    frames_to_ms(*total) - before
}

fn run(mut task: Task) -> Result<Outcome, EngineError> {
    let mut failure = capture(&mut task);

    // Whether the audio still queued belongs to the meeting. Read before the
    // session is told the recording is over: a stop leaves the session exactly
    // as it was so that this question still has the right answer, and the last
    // fraction of a second of a paused meeting is dropped rather than kept.
    let keep_tail = task.session.disposition() == Disposition::Keep;

    // Everything below runs whatever happened above: audio already captured is
    // always owed a finished file.
    let mut audio: Vec<f32> = Vec::new();
    let report = task.source.finish(&mut audio).unwrap_or_else(|error| {
        failure.get_or_insert(error);
        Report::default()
    });

    if keep_tail && !audio.is_empty() {
        let mut tail = 0;
        let milliseconds = advance(&mut tail, audio.len() / 2);
        if let Err(error) = task.writer.write(&audio) {
            failure.get_or_insert(error.to_string());
        }
        // Counted before the session is told the meeting is over.
        task.session.counted(milliseconds);
    }
    let _ = task.session.finish();

    let manifest = task.writer.finish(
        &report.input_device,
        &report.output_device,
        report.start_skew_ms,
    )?;

    // Reopened rather than kept: what the uploader will send is what is on the
    // disk, so the state is written onto the manifest that was read back, and
    // a vault that cannot be reopened is found now rather than at upload time.
    let mut recording = task.vault.reopen(&task.meeting_id)?;
    recording.note_devices(&report.input_device, &report.output_device)?;
    recording.set_state(RecordingState::Uploading)?;

    publish(&task.shared, |snapshot| {
        snapshot.state = AppState::Uploading;
        snapshot.recorded_ms = task.session.recorded().as_millis() as u64;
        snapshot.discarded_ms = task.session.discarded().as_millis() as u64;
        snapshot.microphone_level = 0.0;
        snapshot.system_level = 0.0;
        snapshot.failure.clone_from(&failure);
    });

    Ok(Outcome {
        meeting_id: task.meeting_id,
        debug_id: task.debug_id,
        manifest,
        recorded: task.session.recorded(),
        discarded: task.session.discarded(),
        failure,
    })
}

/// Drain the devices until somebody stops the recording or the budget does.
///
/// Returns what went wrong, if anything. A failure ends the loop and nothing
/// else: the segments already in the vault are the meeting, and they are
/// closed and offered to the uploader exactly as a clean stop would be.
fn capture(task: &mut Task) -> Option<String> {
    let mut audio: Vec<f32> = Vec::with_capacity(TARGET_SAMPLE_RATE as usize * 2);
    let mut kept_frames: u64 = 0;
    let mut dropped_frames: u64 = 0;
    let mut meter_read = Instant::now();
    let mut mic_peak = 0.0_f32;
    let mut system_peak = 0.0_f32;
    let mut failure = None;
    let mut warned = false;
    let mut stop = false;

    while task.source.running() {
        if matches!(commands(task), Flow::Stop) {
            break;
        }

        audio.clear();
        let frames = task.source.poll(&mut audio);
        if frames > 0 {
            let keeping = task.session.disposition() == Disposition::Keep;
            let counter = if keeping {
                &mut kept_frames
            } else {
                &mut dropped_frames
            };
            let milliseconds = advance(counter, frames);

            if keeping {
                if let Err(error) = task.writer.write(&audio) {
                    // Not `?`: returning here would abandon the segments
                    // already in the vault, which are the meeting.
                    failure = Some(error.to_string());
                    break;
                }
            }
            match task.session.counted(milliseconds) {
                BudgetEvent::Stop => stop = true,
                BudgetEvent::Warn => warned = true,
                BudgetEvent::Continue => {}
            }
        }

        let (mic, system) = task.source.levels();
        mic_peak = mic_peak.max(mic);
        system_peak = system_peak.max(system);
        let stalled = task.source.newly_stalled();

        if meter_read.elapsed() >= METER_WINDOW || !stalled.is_empty() || stop {
            let state = task.session.state();
            let recorded = task.session.recorded().as_millis() as u64;
            let discarded = task.session.discarded().as_millis() as u64;
            publish(&task.shared, |snapshot| {
                snapshot.state = state;
                snapshot.recorded_ms = recorded;
                snapshot.discarded_ms = discarded;
                snapshot.microphone_level = mic_peak;
                snapshot.system_level = system_peak;
                snapshot.warned = warned;
                snapshot.stalled.extend(stalled);
            });
            mic_peak = 0.0;
            system_peak = 0.0;
            meter_read = Instant::now();
        }

        if stop {
            break;
        }
        if frames == 0 {
            std::thread::sleep(IDLE_POLL);
        }
    }

    failure
}

/// Whether the loop should carry on after reading its mailbox.
enum Flow {
    Carry,
    Stop,
}

/// Apply every command waiting, and answer each one.
fn commands(task: &mut Task) -> Flow {
    loop {
        match task.requests.try_recv() {
            Ok(request) => {
                let answer = match request.command {
                    Command::Pause => task.session.pause().map(|()| AppState::Paused),
                    Command::Resume => task.session.resume().map(|()| AppState::Recording),
                    Command::Finish => {
                        if task.session.state().may_move_to(AppState::Uploading) {
                            // The reply goes out now and the transition happens
                            // after the tail is drained, so the last fraction
                            // of a second is still billed as recorded.
                            let _ = request.reply.send(Ok(AppState::Uploading));
                            return Flow::Stop;
                        }
                        Err(format!("cannot stop from {:?}", task.session.state()))
                    }
                };
                // The receiver may already be gone if the caller stopped
                // waiting; the command applied, which is what matters.
                let _ = request.reply.send(answer);
            }
            Err(TryRecvError::Empty) => return Flow::Carry,
            // Nobody is left to stop this recording, so it stops itself rather
            // than running until the four-hour ceiling.
            Err(TryRecvError::Disconnected) => return Flow::Stop,
        }
    }
}

/// Update the shared snapshot.
///
/// A poisoned lock is ignored rather than propagated: the recording is worth
/// more than the meter.
fn publish(shared: &Arc<Mutex<Snapshot>>, change: impl FnOnce(&mut Snapshot)) {
    if let Ok(mut snapshot) = shared.lock() {
        change(&mut snapshot);
    }
}
