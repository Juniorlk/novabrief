//! The recording loop, driven end to end without a microphone.
//!
//! These are the tests that could not be written while the rules
//! (`session.rs`) and the audio path (`recording.rs`) were only tested apart:
//! what a pause costs is a property of the loop that joins them, not of either
//! half. The source is a generated tone, so what is exercised here is every
//! line the product runs during a meeting except the WASAPI calls themselves.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

use audio_engine::resample::TARGET_SAMPLE_RATE;
use novabrief_desktop_lib::engine::{Report, Source};
use novabrief_desktop_lib::state::AppState;
use novabrief_desktop_lib::{Declared, Recorder};
use vault::{AccountKey, DpapiSealer, RecordingState, Vault};

const HOUR: Duration = Duration::from_secs(3600);

/// The same secret every time, so a test can reopen what another wrote.
fn account() -> AccountKey {
    AccountKey::derive(b"a-device-secret")
}

fn scratch(name: &str) -> PathBuf {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let unique = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("nb-loop-{name}-{}-{unique}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

fn recorder(root: &Path) -> Recorder {
    Recorder::with_store(Some(root.to_path_buf()), Some(account()))
}

/// A source that produces audio as fast as it is asked for.
///
/// The two channels carry different amplitudes so a test can tell them apart:
/// EF-13 is about noticing that one of them is dead, and a meter that reported
/// the mix would be exactly the meter that cannot.
#[derive(Debug)]
struct Tone {
    /// Stereo frames left to deliver, or `None` for a source that never ends.
    remaining: Option<usize>,
    per_poll: usize,
    phase: usize,
    left: f32,
    right: f32,
    peak_left: f32,
    peak_right: f32,
}

impl Tone {
    fn of(seconds: f64) -> Self {
        Self {
            remaining: Some((seconds * f64::from(TARGET_SAMPLE_RATE)) as usize),
            // A tenth of a second per poll: enough that a test finishes
            // quickly, small enough that pause and resume land mid-stream
            // rather than tidily on a segment boundary.
            per_poll: TARGET_SAMPLE_RATE as usize / 10,
            phase: 0,
            left: 0.9,
            right: 0.4,
            peak_left: 0.0,
            peak_right: 0.0,
        }
    }

    fn endless() -> Self {
        Self {
            remaining: None,
            ..Self::of(0.0)
        }
    }

    fn silent_on_the_right(mut self) -> Self {
        self.right = 0.0;
        self
    }
}

impl Source for Tone {
    fn poll(&mut self, out: &mut Vec<f32>) -> usize {
        let frames = match self.remaining {
            Some(0) => return 0,
            Some(left) => left.min(self.per_poll),
            None => self.per_poll,
        };
        for _ in 0..frames {
            let angle =
                self.phase as f32 * 2.0 * std::f32::consts::PI * 440.0 / TARGET_SAMPLE_RATE as f32;
            let value = angle.sin();
            out.push(value * self.left);
            out.push(value * self.right);
            self.phase += 1;
        }
        self.peak_left = self.peak_left.max(self.left);
        self.peak_right = self.peak_right.max(self.right);
        if let Some(left) = self.remaining.as_mut() {
            *left -= frames;
        }
        frames
    }

    fn levels(&mut self) -> (f32, f32) {
        let levels = (self.peak_left, self.peak_right);
        self.peak_left = 0.0;
        self.peak_right = 0.0;
        levels
    }

    fn running(&self) -> bool {
        self.remaining != Some(0)
    }

    fn newly_stalled(&mut self) -> Vec<String> {
        Vec::new()
    }

    fn finish(&mut self, _out: &mut Vec<f32>) -> Result<Report, String> {
        self.remaining = Some(0);
        Ok(Report {
            input_device: "a test tone".to_owned(),
            output_device: "a test tone".to_owned(),
            start_skew_ms: 0,
        })
    }
}

/// A recording that has never been declared to a server, which is what a
/// meeting recorded with no network is.
fn declared(local: &str) -> Declared {
    Declared {
        local_id: local.to_owned(),
        server_meeting_id: None,
        debug_id: format!("DBG-{local}"),
        started_at: "2026-09-13T10:00:00Z".to_owned(),
    }
}

/// Wait for the recorder to reach `state`, or say what it reached instead.
fn wait_for(recorder: &Recorder, state: AppState) {
    for _ in 0..1000 {
        if recorder.state() == state {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("stuck in {:?}, expected {state:?}", recorder.state());
}

/// Wait until at least `wanted` has been kept, and return what was.
fn wait_until_recorded(recorder: &Recorder, wanted: Duration) -> Duration {
    for _ in 0..2000 {
        let recorded = recorder.recorded();
        if recorded >= wanted {
            return recorded;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    panic!(
        "only {:?} was recorded, wanted {wanted:?}",
        recorder.recorded()
    );
}

#[test]
fn a_new_recorder_is_idle() {
    let root = scratch("idle");
    let recorder = recorder(&root);
    assert_eq!(recorder.state(), AppState::Idle);
    assert_eq!(recorder.recorded(), Duration::ZERO);
}

/// The whole path: a tone goes in, encrypted segments come out of the vault,
/// and the recording is left ready for the uploader.
#[test]
fn a_recording_ends_up_in_the_vault() {
    let root = scratch("vault");
    let recorder = recorder(&root);
    recorder
        .start_with(Box::new(Tone::of(12.0)), HOUR, &declared("mtg-1"))
        .expect("starts");

    wait_for(&recorder, AppState::Uploading);
    let outcome = recorder.take_completed().expect("a finished recording");

    assert!(outcome.failure.is_none(), "{:?}", outcome.failure);
    assert!(
        outcome.manifest.segments.len() >= 2,
        "twelve seconds should be several five-second segments, got {}",
        outcome.manifest.segments.len()
    );
    assert!(
        outcome.recorded >= Duration::from_secs(11),
        "only {:?} of twelve seconds was kept",
        outcome.recorded
    );

    // What is on the disk is what the vault put there, and it decrypts.
    let store = Vault::new(&root, account(), DpapiSealer);
    let recording = store.reopen("mtg-1").expect("reopens");
    assert_eq!(recording.manifest().state, RecordingState::Uploading);
    assert_eq!(
        recording.manifest().input_device,
        "a test tone",
        "the vault manifest was never told which device actually recorded"
    );
    assert_eq!(&recording.read(0).expect("decrypts")[..4], b"OggS");

    let _ = std::fs::remove_dir_all(&root);
}

/// EF-33 through the real loop rather than through the rules alone: audio
/// captured during a pause must not reach the disk, and must not be billed.
#[test]
fn a_pause_keeps_nothing_and_bills_nothing() {
    let root = scratch("pause");
    let recorder = recorder(&root);
    recorder
        .start_with(Box::new(Tone::endless()), HOUR, &declared("mtg-2"))
        .expect("starts");

    let before = wait_until_recorded(&recorder, Duration::from_secs(2));
    recorder.pause().expect("pauses");

    // Long enough that a pause being billed would be unmistakable.
    std::thread::sleep(Duration::from_millis(600));
    assert_eq!(
        recorder.snapshot().expect("recording").state,
        AppState::Paused
    );

    recorder.resume().expect("resumes");
    wait_until_recorded(&recorder, before + Duration::from_secs(2));
    recorder.finish().expect("finishes");

    let outcome = recorder.take_completed().expect("a finished recording");
    assert!(
        outcome.discarded >= Duration::from_millis(400),
        "the pause discarded only {:?}",
        outcome.discarded
    );

    // The kept audio is the encoded audio. A pause that had been stored and
    // merely left out of the count would show up as a longer file than bill.
    let encoded = Duration::from_millis(outcome.manifest.duration_ms);
    assert!(
        encoded.abs_diff(outcome.recorded) < Duration::from_millis(250),
        "the vault holds {encoded:?} but {:?} was billed",
        outcome.recorded
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A pause does not start a second file (EF-33): one vault recording, one
/// unbroken sequence of segment indices.
#[test]
fn pausing_does_not_start_a_second_recording() {
    let root = scratch("onefile");
    let recorder = recorder(&root);
    recorder
        .start_with(Box::new(Tone::endless()), HOUR, &declared("mtg-3"))
        .expect("starts");

    wait_until_recorded(&recorder, Duration::from_secs(6));
    recorder.pause().expect("pauses");
    std::thread::sleep(Duration::from_millis(200));
    recorder.resume().expect("resumes");
    wait_until_recorded(&recorder, Duration::from_secs(12));
    recorder.finish().expect("finishes");

    let outcome = recorder.take_completed().expect("a finished recording");
    let indices: Vec<u32> = outcome.manifest.segments.iter().map(|s| s.index).collect();
    assert_eq!(
        indices,
        (0..indices.len() as u32).collect::<Vec<_>>(),
        "the segments are not one unbroken sequence"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// EF-34: the recording stops itself at the limit, and nobody has to be
/// watching for the result to be collected.
#[test]
fn the_limit_stops_the_recording_on_its_own() {
    let root = scratch("limit");
    let recorder = recorder(&root);
    recorder
        .start_with(
            Box::new(Tone::endless()),
            Duration::from_secs(6),
            &declared("mtg-4"),
        )
        .expect("starts");

    wait_for(&recorder, AppState::Uploading);
    let outcome = recorder.take_completed().expect("a finished recording");
    assert!(
        outcome.recorded >= Duration::from_secs(6),
        "stopped at {:?}, before the limit",
        outcome.recorded
    );
    assert!(
        outcome.recorded < Duration::from_secs(8),
        "ran to {:?}, well past the limit",
        outcome.recorded
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// EF-13 is "a flat system meter is noticed in under five seconds". That only
/// works if the two meters are read separately: one mixed level, on a machine
/// with a live microphone, looks healthy with no system audio at all.
#[test]
fn the_two_meters_are_read_separately() {
    let root = scratch("meters");
    let recorder = recorder(&root);
    recorder
        .start_with(
            Box::new(Tone::endless().silent_on_the_right()),
            HOUR,
            &declared("mtg-5"),
        )
        .expect("starts");

    let mut seen = None;
    for _ in 0..500 {
        let snapshot = recorder.snapshot().expect("recording");
        if snapshot.microphone_level > 0.0 {
            seen = Some(snapshot);
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let snapshot = seen.expect("the microphone meter never moved");
    assert!(snapshot.microphone_level > 0.5);
    assert_eq!(
        snapshot.system_level, 0.0,
        "the system meter moved for audio that was never there"
    );

    recorder.finish().expect("finishes");
    let _ = std::fs::remove_dir_all(&root);
}

/// Starting twice would abandon the first meeting, which is the one failure a
/// person cannot recover from.
#[test]
fn a_second_recording_cannot_displace_the_first() {
    let root = scratch("second");
    let recorder = recorder(&root);
    recorder
        .start_with(Box::new(Tone::endless()), HOUR, &declared("mtg-6"))
        .expect("starts");

    let message = recorder
        .start_with(Box::new(Tone::endless()), HOUR, &declared("mtg-7"))
        .expect_err("already recording");
    assert!(message.contains("Recording"), "{message}");

    recorder.finish().expect("finishes");
    let _ = std::fs::remove_dir_all(&root);
}

/// A refused operation leaves the recording alone. Applying it and reporting
/// an error would be worse than either.
#[test]
fn a_refused_operation_changes_nothing() {
    let root = scratch("refused");
    let recorder = recorder(&root);
    assert!(recorder.pause().is_err(), "nothing is being recorded");
    assert_eq!(recorder.state(), AppState::Idle);

    recorder
        .start_with(Box::new(Tone::endless()), HOUR, &declared("mtg-8"))
        .expect("starts");
    assert!(recorder.resume().is_err(), "it is not paused");
    assert_eq!(recorder.state(), AppState::Recording);

    recorder.finish().expect("finishes");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn the_error_says_what_was_being_asked_of_nothing() {
    let root = scratch("nothing");
    let recorder = recorder(&root);
    let message = recorder.finish().expect_err("nothing to finish");
    assert!(message.contains("nothing is being recorded"), "{message}");
}

/// The same path, on the machine's real devices.
///
/// Ignored by default because it needs a microphone and a render device, which
/// a CI runner does not have. Everything above proves the loop; only this
/// proves that the loop and WASAPI agree, so it is run by hand and its result
/// belongs in the task report rather than in a green tick.
#[test]
#[ignore = "opens the real audio devices"]
// The printed line is this test result, not a leftover debug statement: what
// it proves is which devices Windows actually opened and what the meters read
// on them, and neither is something an assertion can carry into a report.
#[allow(clippy::print_stdout)]
fn real_devices_record_into_the_vault() {
    let root = scratch("real");
    let recorder = recorder(&root);
    recorder
        .start(
            HOUR,
            &declared("mtg-real"),
            &audio_engine::pipeline::Endpoints::default(),
        )
        .expect("the audio devices open");

    // Long enough for two five-second segments, so the segmenting is exercised
    // rather than only the tail. The meters are sampled all the way through:
    // one reading at the end covers a tenth of a second and would report a
    // silent instant as a dead channel.
    let mut loudest_microphone = 0.0_f32;
    let mut loudest_system = 0.0_f32;
    for _ in 0..120 {
        let live = recorder.snapshot().expect("recording");
        loudest_microphone = loudest_microphone.max(live.microphone_level);
        loudest_system = loudest_system.max(live.system_level);
        std::thread::sleep(Duration::from_millis(100));
    }
    recorder.finish().expect("finishes");

    let outcome = recorder.take_completed().expect("a finished recording");
    println!(
        "recorded {:?} from {} and {}, {} segments, loudest meters {loudest_microphone:.6} / {loudest_system:.6}",
        outcome.recorded,
        outcome.manifest.input_device,
        outcome.manifest.output_device,
        outcome.manifest.segments.len(),
    );

    assert!(outcome.failure.is_none(), "{:?}", outcome.failure);
    assert!(
        outcome.recorded >= Duration::from_secs(11),
        "twelve seconds of wall clock produced only {:?}",
        outcome.recorded
    );

    let store = Vault::new(&root, account(), DpapiSealer);
    let recording = store.reopen("mtg-real").expect("reopens");
    assert_eq!(recording.manifest().state, RecordingState::Uploading);
    assert_eq!(&recording.read(0).expect("decrypts")[..4], b"OggS");

    let _ = std::fs::remove_dir_all(&root);
}
