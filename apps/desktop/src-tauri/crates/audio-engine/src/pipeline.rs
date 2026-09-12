//! Both endpoints, mixed into one stereo stream, as a thing the product can run.
//!
//! This loop used to live only in `nb-capture`, the measurement tool. That was
//! the wrong place for it: the tool is what produced the "0.000 % loss over
//! 60 minutes" figure, and if the desktop had its own copy of the loop, that
//! figure would describe the tool rather than the product. They are the same
//! code now, so measuring one measures the other.
//!
//! What the caller gets is frames, not files. Where they go - an Ogg file for
//! the measurement tool, the encrypted vault for the product - is the caller's
//! business, the same separation the encoder already has.

use std::sync::atomic::AtomicBool;
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use crate::capture::{CaptureStats, EndpointCapture};
use crate::drift::{DriftEstimate, DriftEstimator, DriftRefusal};
use crate::error::{CaptureError, Endpoint, Result};
use crate::format::{downmix_to_mono, peak};
use crate::resample::{MonoResampler, StereoMixer, TARGET_SAMPLE_RATE};

/// How far a capture thread may run ahead of the mixer.
///
/// The queues were unbounded once, and that is half of how an hour-long
/// recording lost 10.25 % of its audio: when the writer fell behind nothing
/// pushed back, memory grew, the capture threads missed their deadlines, and
/// WASAPI's buffer overran - which is where the audio actually went.
///
/// A chunk is one WASAPI packet resampled to 16 kHz mono, roughly 10 ms. Three
/// hundred is about three seconds: long enough to ride out a disk hiccup, short
/// enough that a consumer which has stopped is noticed in seconds rather than
/// in gigabytes.
///
/// Blocking on a full queue would be worse than dropping. The capture thread
/// has a hard deadline against the device, and making it wait is how frames are
/// lost at the source, where nothing can recover them.
pub const QUEUE_DEPTH_CHUNKS: usize = 300;

/// A chunk of 16 kHz mono audio on its way to the mixer.
#[derive(Debug)]
struct Chunk {
    samples: Vec<f32>,
    /// When this endpoint delivered its very first frame, repeated on every
    /// chunk so the mixer can align the two streams as soon as both have spoken.
    first_qpc_100ns: Option<u64>,
}

/// Audio a full queue forced us to drop.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Overflow {
    /// Chunks dropped.
    pub chunks: u64,
    /// Frames of audio inside them.
    pub frames: u64,
}

impl Overflow {
    /// Whether anything was dropped at all.
    #[must_use]
    pub const fn happened(&self) -> bool {
        self.chunks > 0
    }
}

/// What became of an item offered to a bounded queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Offered {
    Taken,
    Dropped { first: bool },
    Closed,
}

/// The sending half of a bounded queue, which counts what it had to drop.
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
    /// `frames` is what it is worth in audio, so the counter measures lost
    /// *audio* rather than lost messages - the number anyone reading a report
    /// actually cares about.
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

/// How much silence each channel needs so neither can run away from the other.
///
/// Returns `(left, right)` frames of silence to push.
///
/// The mixer only emits while **both** sides have data, which is what keeps the
/// two voices aligned. The corollary is that a side which stops pins the other
/// in memory, and that is not a theory: a 60-minute validation capture had the
/// system loopback stop at minute 28, and the microphone backlog then grew by
/// one second of audio per second of meeting - 34.8 MB by minute 35, on its way
/// past a gigabyte over the four hours EF-34 allows. Nothing reported it.
///
/// `live` is about the thread, not the configuration. Padding a side nobody
/// asked to record is a different condition, and it never covered a side that
/// was being recorded and then ended.
#[must_use]
fn silence_needed(imbalance: i64, left_live: bool, right_live: bool) -> (usize, usize) {
    if left_live && right_live {
        return (0, 0);
    }
    match imbalance.cmp(&0) {
        std::cmp::Ordering::Greater if !right_live => {
            (0, usize::try_from(imbalance).unwrap_or(usize::MAX))
        }
        std::cmp::Ordering::Less if !left_live => {
            (usize::try_from(-imbalance).unwrap_or(usize::MAX), 0)
        }
        _ => (0, 0),
    }
}

/// Which endpoints to record.
#[derive(Debug, Clone, Default)]
pub struct Endpoints {
    /// Microphone device, by substring. `None` is the Windows default.
    pub microphone: Option<String>,
    /// Render device to capture in loopback. `None` is the Windows default.
    pub system: Option<String>,
    /// Leave the microphone out entirely.
    pub without_microphone: bool,
    /// Leave the system audio out entirely.
    pub without_system: bool,
}

/// What one endpoint reported when it stopped.
#[derive(Debug)]
pub struct EndpointOutcome {
    /// Which endpoint.
    pub endpoint: Endpoint,
    /// The device it actually opened.
    pub device_name: String,
    /// Rate the device negotiated.
    pub input_rate: u32,
    /// Channels the device negotiated.
    pub input_channels: u16,
    /// Whether the thread got the Pro Audio scheduling class.
    pub mmcss: bool,
    /// Frames, discontinuities, synthesised silence, device reopens.
    pub stats: CaptureStats,
    /// Audio a full queue forced us to drop.
    pub overflow: Overflow,
    /// Wall-clock time the capture loop ran.
    pub elapsed: Duration,
    /// How fast this device's clock actually ran, or why that cannot be said.
    pub drift: std::result::Result<DriftEstimate, DriftRefusal>,
    /// Timestamp and cumulative frame count at the first delivered packet.
    pub first_packet: Option<(u64, u64)>,
    /// The same, at the most recent one.
    pub last_packet: Option<(u64, u64)>,
}

/// Both endpoints, running.
#[derive(Debug)]
pub struct Recorder {
    stop: Arc<AtomicBool>,
    mic: Option<JoinHandle<Result<EndpointOutcome>>>,
    system: Option<JoinHandle<Result<EndpointOutcome>>>,
    mic_rx: Receiver<Chunk>,
    system_rx: Receiver<Chunk>,
    mixer: StereoMixer,

    mic_recorded: bool,
    system_recorded: bool,
    mic_open: bool,
    system_open: bool,
    mic_start: Option<u64>,
    system_start: Option<u64>,
    applied_skew: Option<i64>,

    mic_peak: f32,
    system_peak: f32,
    worst_imbalance: i64,
    stalled: Vec<Endpoint>,
}

impl Recorder {
    /// Open the endpoints and start capturing.
    ///
    /// # Errors
    ///
    /// [`CaptureError`] if a requested device cannot be opened. A device that
    /// fails later is reported through [`Recorder::finish`] instead, because by
    /// then there is audio worth keeping.
    pub fn start(endpoints: &Endpoints, max_duration: Option<Duration>) -> Result<Self> {
        let stop = Arc::new(AtomicBool::new(false));
        let (mic_tx, mic_rx) = sync_channel::<Chunk>(QUEUE_DEPTH_CHUNKS);
        let (system_tx, system_rx) = sync_channel::<Chunk>(QUEUE_DEPTH_CHUNKS);

        let mic_recorded = !endpoints.without_microphone;
        let system_recorded = !endpoints.without_system;

        let mic = mic_recorded.then(|| {
            spawn(
                Endpoint::Microphone,
                endpoints.microphone.clone(),
                Arc::clone(&stop),
                max_duration,
                mic_tx,
            )
        });
        let system = system_recorded.then(|| {
            spawn(
                Endpoint::SystemLoopback,
                endpoints.system.clone(),
                Arc::clone(&stop),
                max_duration,
                system_tx,
            )
        });

        Ok(Self {
            stop,
            mic,
            system,
            mic_rx,
            system_rx,
            mixer: StereoMixer::new(),
            mic_recorded,
            system_recorded,
            mic_open: mic_recorded,
            system_open: system_recorded,
            mic_start: None,
            system_start: None,
            applied_skew: None,
            mic_peak: 0.0,
            system_peak: 0.0,
            worst_imbalance: 0,
            stalled: Vec::new(),
        })
    }

    /// Whether either endpoint is still delivering.
    #[must_use]
    pub const fn running(&self) -> bool {
        self.mic_open || self.system_open
    }

    /// Drain whatever has arrived and append the stereo frames it completes.
    ///
    /// Interleaved, left = microphone, right = system audio (EF-31). Returns
    /// how many stereo frames were appended, which is zero whenever one side is
    /// waiting for its counterpart.
    ///
    /// Appends rather than returns a `Vec`, so a caller polling ten times a
    /// second is not allocating ten times a second.
    pub fn poll(&mut self, out: &mut Vec<f32>) -> usize {
        self.drain();
        self.correct_skew();
        self.pad_stalled_sides();
        self.observe_imbalance();

        let before = out.len();
        self.mixer.drain_into(out);
        (out.len() - before) / 2
    }

    fn drain(&mut self) {
        loop {
            match self.mic_rx.try_recv() {
                Ok(chunk) => {
                    self.mic_start = self.mic_start.or(chunk.first_qpc_100ns);
                    self.mic_peak = self.mic_peak.max(peak(&chunk.samples));
                    self.mixer.push_left(&chunk.samples);
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    if self.mic_open && self.mic_recorded {
                        self.stalled.push(Endpoint::Microphone);
                    }
                    self.mic_open = false;
                    break;
                }
            }
        }
        loop {
            match self.system_rx.try_recv() {
                Ok(chunk) => {
                    self.system_start = self.system_start.or(chunk.first_qpc_100ns);
                    self.system_peak = self.system_peak.max(peak(&chunk.samples));
                    self.mixer.push_right(&chunk.samples);
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    if self.system_open && self.system_recorded {
                        self.stalled.push(Endpoint::SystemLoopback);
                    }
                    self.system_open = false;
                    break;
                }
            }
        }
    }

    /// Line the two streams up, once, as soon as both have said when they
    /// started.
    ///
    /// The devices never open at the same instant, and how far apart is a
    /// property of the machine - so it is measured from the shared performance
    /// counter rather than assumed. The stream that started later is missing
    /// audio at the front, so it gets that much silence.
    fn correct_skew(&mut self) {
        if self.applied_skew.is_some() || !(self.mic_recorded && self.system_recorded) {
            return;
        }
        let (Some(mic_qpc), Some(system_qpc)) = (self.mic_start, self.system_start) else {
            return;
        };

        let delta_100ns = i128::from(mic_qpc) - i128::from(system_qpc);
        let frames = (delta_100ns * i128::from(TARGET_SAMPLE_RATE)) / 10_000_000;
        let frames = i64::try_from(frames).unwrap_or(0);
        match frames.cmp(&0) {
            std::cmp::Ordering::Greater => {
                self.mixer.push_front_left(&vec![0.0; frames as usize]);
            }
            std::cmp::Ordering::Less => {
                self.mixer
                    .push_front_right(&vec![0.0; frames.unsigned_abs() as usize]);
            }
            std::cmp::Ordering::Equal => {}
        }
        self.applied_skew = Some(frames);
    }

    fn pad_stalled_sides(&mut self) {
        let (left, right) = silence_needed(
            self.mixer.imbalance(),
            self.mic_recorded && self.mic_open,
            self.system_recorded && self.system_open,
        );
        if left > 0 {
            self.mixer.push_left(&vec![0.0; left]);
        }
        if right > 0 {
            self.mixer.push_right(&vec![0.0; right]);
        }
    }

    /// Only while both are running and the skew has been corrected. Outside
    /// that window the number is an artefact: before the correction it is
    /// dominated by the startup difference, and after one stream ends it simply
    /// grows by however long the other keeps going.
    fn observe_imbalance(&mut self) {
        let settled = self.applied_skew.is_some() || !(self.mic_recorded && self.system_recorded);
        if settled
            && self.mic_open
            && self.system_open
            && self.mixer.imbalance().abs() > self.worst_imbalance.abs()
        {
            self.worst_imbalance = self.mixer.imbalance();
        }
    }

    /// Peak level on each channel since the last call, as `(microphone, system)`.
    ///
    /// Reset by reading, so a meter drawn ten times a second shows the loudest
    /// moment of each hundredth of a second rather than of the whole meeting.
    /// EF-13 turns on this: a flat system meter is how somebody notices within
    /// five seconds that the remote voices are not being captured.
    pub fn levels(&mut self) -> (f32, f32) {
        let levels = (self.mic_peak, self.system_peak);
        self.mic_peak = 0.0;
        self.system_peak = 0.0;
        levels
    }

    /// Endpoints that stopped delivering since the last call.
    ///
    /// Reported as they happen rather than at the end: the rest of a meeting
    /// recorded with an empty channel is worth knowing about while it is still
    /// happening.
    pub fn newly_stalled(&mut self) -> Vec<Endpoint> {
        std::mem::take(&mut self.stalled)
    }

    /// The worst divergence seen while both streams were running, in frames.
    #[must_use]
    pub const fn worst_imbalance(&self) -> i64 {
        self.worst_imbalance
    }

    /// The start skew that was corrected, in frames, if it could be measured.
    ///
    /// `None` and `Some(0)` are different answers and must stay so. A skew of
    /// exactly zero is a measurement; `None` means one endpoint never delivered
    /// a real packet, which happens whenever nothing is playing on the machine -
    /// and reporting that as "no skew" would claim the two streams are aligned
    /// when nobody checked.
    #[must_use]
    pub const fn applied_skew(&self) -> Option<i64> {
        self.applied_skew
    }

    /// Stop both endpoints, flush the tail, and collect what they reported.
    ///
    /// The tail is padded on the shorter side, so the last few frames of the
    /// meeting are not dropped for want of a counterpart.
    ///
    /// # Errors
    ///
    /// [`CaptureError`] if an endpoint failed. The frames appended to `out`
    /// before the failure are still valid audio.
    pub fn finish(mut self, out: &mut Vec<f32>) -> Result<Vec<EndpointOutcome>> {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);

        // Keep draining until both threads have closed their channels,
        // otherwise a thread blocked on a full queue would never see the flag.
        while self.running() {
            self.poll(out);
            std::thread::sleep(Duration::from_millis(5));
        }
        self.mixer.flush_into(out);

        let mut outcomes = Vec::new();
        for handle in [self.mic.take(), self.system.take()].into_iter().flatten() {
            match handle.join() {
                Ok(result) => outcomes.push(result?),
                Err(_) => {
                    return Err(CaptureError::ThreadPanicked);
                }
            }
        }
        Ok(outcomes)
    }
}

fn spawn(
    endpoint: Endpoint,
    device: Option<String>,
    stop: Arc<AtomicBool>,
    max_duration: Option<Duration>,
    tx: SyncSender<Chunk>,
) -> JoinHandle<Result<EndpointOutcome>> {
    std::thread::spawn(move || -> Result<EndpointOutcome> {
        let mut capture = EndpointCapture::open_named(endpoint, device.as_deref())?;

        let format = capture.format();
        let mut resampler = MonoResampler::new(format.sample_rate);
        let mut mono = Vec::new();
        let mut converted = Vec::new();
        let mut first_qpc: Option<u64> = None;
        let mut device_frames: u64 = 0;
        let mut first_packet: Option<(u64, u64)> = None;
        let mut last_packet: Option<(u64, u64)> = None;
        let mut sink = BoundedSink::new(tx);
        let mut estimator = DriftEstimator::new(format.sample_rate);
        let started = std::time::Instant::now();

        let stats = capture.record(&stop, max_duration, |packet| {
            if first_qpc.is_none() {
                first_qpc = packet.stream_start_qpc_100ns;
            }

            let packet_frames = (packet.samples.len() / format.channels.max(1) as usize) as u64;
            if packet.synthesised {
                // Counted, never fitted: silence we invented is timed by our own
                // clock, so fitting it would compare that clock with itself. How
                // much of it there is decides whether the fit means anything.
                estimator.note_synthesised(packet_frames);
            } else {
                if let Some(qpc) = packet.qpc_100ns {
                    estimator.observe(qpc, device_frames);
                    if first_packet.is_none() {
                        first_packet = Some((qpc, device_frames));
                    }
                    last_packet = Some((qpc, device_frames));
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
                sink.offer(chunk, frames);
            }
        })?;

        Ok(EndpointOutcome {
            endpoint,
            device_name: capture.device_name().to_owned(),
            input_rate: format.sample_rate,
            input_channels: format.channels,
            mmcss: capture.mmcss_active(),
            stats,
            overflow: sink.overflow,
            elapsed: started.elapsed(),
            drift: estimator.estimate(),
            first_packet,
            last_packet,
        })
    })
}

#[cfg(test)]
mod tests {
    use super::{silence_needed, BoundedSink, Offered, Overflow};
    use std::sync::mpsc::sync_channel;

    /// A producer faster than its consumer must leave memory bounded and say
    /// that it did.
    #[test]
    fn a_producer_faster_than_its_consumer_is_bounded_and_counted() {
        const DEPTH: usize = 8;
        const OFFERED: usize = 1_000;
        const FRAMES_EACH: u64 = 160;

        let (tx, rx) = sync_channel::<u32>(DEPTH);
        let mut sink = BoundedSink::new(tx);

        let mut taken = 0_usize;
        for item in 0..OFFERED {
            match sink.offer(item as u32, FRAMES_EACH) {
                Offered::Taken => taken += 1,
                Offered::Dropped { .. } => {}
                Offered::Closed => panic!("the receiver is still alive"),
            }
        }

        assert_eq!(taken, DEPTH, "the queue accepted more than its depth");
        assert_eq!(rx.try_iter().count(), DEPTH);
        assert_eq!(sink.overflow.chunks as usize, OFFERED - DEPTH);
        assert_eq!(
            sink.overflow.frames,
            (OFFERED - DEPTH) as u64 * FRAMES_EACH,
            "the counter must measure lost audio, not lost messages"
        );
    }

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

    #[test]
    fn a_closed_consumer_is_not_counted_as_loss() {
        let (tx, rx) = sync_channel::<u32>(4);
        let mut sink = BoundedSink::new(tx);
        drop(rx);

        assert_eq!(sink.offer(0, 160), Offered::Closed);
        assert_eq!(sink.overflow, Overflow::default());
    }

    /// The side that stopped is padded rather than waited for.
    #[test]
    fn a_side_that_stops_is_padded_rather_than_waited_for() {
        assert_eq!(silence_needed(160_000, true, false), (0, 160_000));
        assert_eq!(silence_needed(-160_000, false, true), (160_000, 0));
    }

    /// While both run the mixer holds the surplus on purpose: that is what
    /// keeps the two voices aligned, and the backlog is bounded by the
    /// difference between two device clocks, which is milliseconds.
    #[test]
    fn a_healthy_lead_is_left_alone() {
        assert_eq!(silence_needed(4_000, true, true), (0, 0));
        assert_eq!(silence_needed(-4_000, true, true), (0, 0));
    }

    #[test]
    fn a_stopped_side_that_is_already_behind_needs_nothing() {
        assert_eq!(silence_needed(-5_000, true, false), (0, 0));
        assert_eq!(silence_needed(0, true, false), (0, 0));
    }
}
