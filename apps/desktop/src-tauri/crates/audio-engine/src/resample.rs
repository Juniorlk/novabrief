//! Resampling every endpoint to the pipeline's working rate.
//!
//! Both streams must land on a common clock before they can share a stereo
//! frame, and speech transcription providers want 16 kHz mono. The microphone
//! and the render endpoint routinely disagree — 44.1 kHz against 48 kHz is the
//! everyday case — so each one is resampled independently.
//!
//! Sinc interpolation is used rather than a cheap polynomial: this audio is fed
//! to a speech-to-text engine, and aliasing introduced here would be blamed on
//! the transcription provider later.

use rubato::{
    Resampler, SincFixedIn, SincInterpolationParameters, SincInterpolationType, WindowFunction,
};

/// Working sample rate of the whole pipeline, in Hz.
///
/// 16 kHz is what AssemblyAI, Deepgram and Whisper all consume for speech, and
/// it keeps an hour of stereo Opus around 14 MB.
pub const TARGET_SAMPLE_RATE: u32 = 16_000;

/// Input frames consumed per resampler call.
const CHUNK: usize = 1024;

/// Dead prefix tolerated before the pending buffer is compacted.
const COMPACT_THRESHOLD: usize = CHUNK * 8;

/// Resamples one mono stream to [`TARGET_SAMPLE_RATE`].
///
/// Input arrives in whatever sizes WASAPI hands out, so the incoming samples are
/// buffered and consumed in fixed chunks; whatever does not fill a chunk waits
/// for the next call rather than being dropped.
pub struct MonoResampler {
    inner: Option<SincFixedIn<f32>>,
    pending: Vec<f32>,
    /// How much of `pending` has already been converted.
    ///
    /// Same reasoning as the encoder: `drain(..CHUNK)` shifts the whole tail,
    /// so a resampler that is behind pays O(n) per chunk and O(n squared) over
    /// a backlog. A cursor moves nothing, and the tail is compacted only when
    /// the dead prefix has earned the copy.
    head: usize,
    /// Samples physically copied by compaction. See the encoder's field of the
    /// same name: this is the quantity that grew quadratically.
    moved: u64,
    scratch_in: Vec<Vec<f32>>,
    scratch_out: Vec<Vec<f32>>,
    input_rate: u32,
}

impl std::fmt::Debug for MonoResampler {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MonoResampler")
            .field("input_rate", &self.input_rate)
            .field("passthrough", &self.inner.is_none())
            .field("pending", &self.pending.len())
            .finish()
    }
}

impl MonoResampler {
    /// Build a resampler from `input_rate` to [`TARGET_SAMPLE_RATE`].
    ///
    /// When the input is already at the target rate the resampler is skipped
    /// entirely: no interpolation, no added delay, no wasted CPU.
    #[must_use]
    pub fn new(input_rate: u32) -> Self {
        if input_rate == TARGET_SAMPLE_RATE || input_rate == 0 {
            return Self {
                inner: None,
                pending: Vec::new(),
                head: 0,
                moved: 0,
                scratch_in: Vec::new(),
                scratch_out: Vec::new(),
                input_rate,
            };
        }

        let parameters = SincInterpolationParameters {
            sinc_len: 256,
            f_cutoff: 0.95,
            interpolation: SincInterpolationType::Linear,
            oversampling_factor: 256,
            window: WindowFunction::BlackmanHarris2,
        };
        let ratio = f64::from(TARGET_SAMPLE_RATE) / f64::from(input_rate);

        // A construction failure here means the ratio is out of range, which we
        // have just computed from two positive rates. Falling back to
        // passthrough keeps the recording alive rather than aborting it; the
        // rate mismatch is then visible in the output file.
        let inner = SincFixedIn::<f32>::new(ratio, 1.0, parameters, CHUNK, 1).ok();

        Self {
            inner,
            pending: Vec::new(),
            head: 0,
            moved: 0,
            scratch_in: vec![Vec::with_capacity(CHUNK)],
            scratch_out: Vec::new(),
            input_rate,
        }
    }

    /// True when samples pass through untouched because no conversion is needed.
    #[must_use]
    pub const fn is_passthrough(&self) -> bool {
        self.inner.is_none()
    }

    /// The rate this resampler converts from.
    #[must_use]
    pub const fn input_rate(&self) -> u32 {
        self.input_rate
    }

    /// Feed mono samples in, append converted mono samples to `out`.
    pub fn process(&mut self, input: &[f32], out: &mut Vec<f32>) {
        let Some(resampler) = self.inner.as_mut() else {
            out.extend_from_slice(input);
            return;
        };

        self.pending.extend_from_slice(input);

        while self.pending.len() - self.head >= CHUNK {
            // Copied into the scratch buffer the resampler already owns rather
            // than collected into a fresh Vec: one allocation per chunk is one
            // allocation every few milliseconds, for the whole meeting.
            let chunk = &self.pending[self.head..self.head + CHUNK];
            match self.scratch_in.first_mut() {
                Some(buffer) => {
                    buffer.clear();
                    buffer.extend_from_slice(chunk);
                }
                None => self.scratch_in.push(chunk.to_vec()),
            }
            self.head += CHUNK;

            match resampler.process(&self.scratch_in, None) {
                Ok(converted) => {
                    self.scratch_out = converted;
                    if let Some(channel) = self.scratch_out.first() {
                        out.extend_from_slice(channel);
                    }
                }
                Err(_) => {
                    // A refused chunk must not desynchronise the timeline, so
                    // the equivalent duration is emitted as silence instead.
                    let expected = (CHUNK as f64 * f64::from(TARGET_SAMPLE_RATE)
                        / f64::from(self.input_rate)) as usize;
                    out.extend(std::iter::repeat_n(0.0, expected));
                }
            }
        }

        // Same amortisation as the encoder: nothing moves in the steady state,
        // and a sample is copied at most once per doubling of the backlog.
        if self.head > 0
            && (self.head >= self.pending.len() - self.head || self.head >= COMPACT_THRESHOLD)
        {
            self.moved += (self.pending.len() - self.head) as u64;
            self.pending.copy_within(self.head.., 0);
            self.pending.truncate(self.pending.len() - self.head);
            self.head = 0;
        }
    }

    /// Samples moved by compaction so far.
    #[must_use]
    pub const fn samples_moved(&self) -> u64 {
        self.moved
    }

    /// Samples waiting to be converted. Exposed so a test can prove the buffer
    /// does not grow without bound when the caller runs ahead.
    #[must_use]
    pub fn buffered(&self) -> usize {
        self.pending.len() - self.head
    }
}

/// Interleaves two mono streams into stereo: left = microphone, right = system.
///
/// The two captures are independent threads with independent clocks, so one
/// side is always slightly ahead. Frames are emitted only while both sides have
/// data; the surplus stays buffered until its counterpart arrives. That keeps
/// the two voices aligned instead of letting the file drift apart, and the
/// backlog is observable through [`StereoMixer::imbalance`], which is the raw
/// material for the drift compensator.
#[derive(Debug, Default)]
pub struct StereoMixer {
    left: std::collections::VecDeque<f32>,
    right: std::collections::VecDeque<f32>,
}

impl StereoMixer {
    /// A mixer with both sides empty.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Queue microphone samples (left channel).
    pub fn push_left(&mut self, samples: &[f32]) {
        self.left.extend(samples);
    }

    /// Queue system samples (right channel).
    pub fn push_right(&mut self, samples: &[f32]) {
        self.right.extend(samples);
    }

    /// Insert samples at the *front* of the microphone channel.
    ///
    /// Used once, to correct the measured start skew between the two endpoints:
    /// the stream that opened later is missing audio at the beginning, and
    /// prepending that much silence puts both channels back on a common
    /// timeline. Prepending only makes sense before playback has been drained,
    /// which is why it happens as soon as both start timestamps are known.
    pub fn push_front_left(&mut self, samples: &[f32]) {
        for &sample in samples.iter().rev() {
            self.left.push_front(sample);
        }
    }

    /// Insert samples at the front of the system channel. See
    /// [`StereoMixer::push_front_left`].
    pub fn push_front_right(&mut self, samples: &[f32]) {
        for &sample in samples.iter().rev() {
            self.right.push_front(sample);
        }
    }

    /// How many frames one side is ahead of the other.
    ///
    /// Positive means the microphone is ahead, negative means the system is.
    #[must_use]
    pub fn imbalance(&self) -> i64 {
        self.left.len() as i64 - self.right.len() as i64
    }

    /// Drain every frame for which both channels have a sample, appending
    /// interleaved stereo to `out`.
    pub fn drain_into(&mut self, out: &mut Vec<f32>) {
        let frames = self.left.len().min(self.right.len());
        out.reserve(frames * 2);
        for _ in 0..frames {
            let (Some(left), Some(right)) = (self.left.pop_front(), self.right.pop_front()) else {
                break;
            };
            out.push(left);
            out.push(right);
        }
    }

    /// Flush what remains once capture has stopped, padding the shorter side
    /// with silence so no captured audio is thrown away at the end.
    pub fn flush_into(&mut self, out: &mut Vec<f32>) {
        let frames = self.left.len().max(self.right.len());
        out.reserve(frames * 2);
        for _ in 0..frames {
            out.push(self.left.pop_front().unwrap_or(0.0));
            out.push(self.right.pop_front().unwrap_or(0.0));
        }
    }
}

#[cfg(test)]
mod tests {

    /// D1, the resampler's half: same quadratic drain, same generous bound.
    #[test]
    fn compaction_stays_linear_under_backlog() {
        let mut resampler = MonoResampler::new(48_000);
        let mut out = Vec::new();

        const BURSTS: usize = 20;
        let burst = vec![0.0_f32; CHUNK * 200 + 7];
        for _ in 0..BURSTS {
            resampler.process(&burst, &mut out);
        }

        let fed = (burst.len() * BURSTS) as u64;
        assert!(
            resampler.samples_moved() <= fed * 2,
            "compaction moved {} samples for {fed} fed - that is superlinear",
            resampler.samples_moved()
        );
    }

    /// Whatever the backlog, the buffer keeps less than one chunk once the
    /// caller stops feeding it: growth is bounded by the input, not by time.
    #[test]
    fn the_buffer_never_keeps_more_than_a_chunk() {
        let mut resampler = MonoResampler::new(44_100);
        let mut out = Vec::new();

        for _ in 0..200 {
            resampler.process(&vec![0.1_f32; CHUNK * 3 + 11], &mut out);
            assert!(
                resampler.buffered() < CHUNK,
                "buffered {} samples, which is more than one chunk",
                resampler.buffered()
            );
        }
    }

    use super::{MonoResampler, StereoMixer, CHUNK, TARGET_SAMPLE_RATE};

    #[test]
    fn matching_rate_is_passthrough() {
        let mut resampler = MonoResampler::new(TARGET_SAMPLE_RATE);
        assert!(resampler.is_passthrough());

        let mut out = Vec::new();
        resampler.process(&[0.1, 0.2, 0.3], &mut out);
        assert_eq!(out, vec![0.1, 0.2, 0.3]);
    }

    #[test]
    fn downsampling_48k_to_16k_yields_about_a_third_of_the_samples() {
        let mut resampler = MonoResampler::new(48_000);
        assert!(!resampler.is_passthrough());

        // One second of 48 kHz input.
        let input: Vec<f32> = (0..48_000).map(|n| (n as f32 * 0.01).sin() * 0.5).collect();
        let mut out = Vec::new();
        resampler.process(&input, &mut out);

        // Sinc resampling holds back a little tail, so allow a small margin
        // around the 16 000 expected samples.
        assert!(
            (15_000..=16_500).contains(&out.len()),
            "expected roughly 16000 samples, got {}",
            out.len()
        );
    }

    #[test]
    fn resampling_44100_to_16k_produces_output() {
        let mut resampler = MonoResampler::new(44_100);
        let input: Vec<f32> = (0..44_100).map(|n| (n as f32 * 0.01).sin()).collect();
        let mut out = Vec::new();
        resampler.process(&input, &mut out);
        assert!(
            (15_000..=16_500).contains(&out.len()),
            "expected roughly 16000 samples, got {}",
            out.len()
        );
    }

    #[test]
    fn partial_chunks_are_buffered_not_dropped() {
        let mut resampler = MonoResampler::new(48_000);
        let mut out = Vec::new();
        // Well under one chunk: nothing can come out yet, and nothing is lost.
        resampler.process(&[0.0; 100], &mut out);
        assert!(out.is_empty());

        resampler.process(&[0.0; 4000], &mut out);
        assert!(!out.is_empty(), "buffered samples should be emitted later");
    }

    #[test]
    fn mixer_interleaves_left_and_right() {
        let mut mixer = StereoMixer::new();
        mixer.push_left(&[1.0, 2.0]);
        mixer.push_right(&[-1.0, -2.0]);

        let mut out = Vec::new();
        mixer.drain_into(&mut out);
        assert_eq!(out, vec![1.0, -1.0, 2.0, -2.0]);
    }

    #[test]
    fn mixer_holds_back_the_side_that_is_ahead() {
        let mut mixer = StereoMixer::new();
        mixer.push_left(&[1.0, 2.0, 3.0]);
        mixer.push_right(&[-1.0]);

        let mut out = Vec::new();
        mixer.drain_into(&mut out);
        assert_eq!(out, vec![1.0, -1.0], "only complete frames are emitted");
        assert_eq!(mixer.imbalance(), 2, "the microphone is two frames ahead");
    }

    #[test]
    fn flush_pads_the_shorter_side_rather_than_discarding_audio() {
        let mut mixer = StereoMixer::new();
        mixer.push_left(&[1.0, 2.0]);
        mixer.push_right(&[-1.0]);

        let mut out = Vec::new();
        mixer.flush_into(&mut out);
        assert_eq!(out, vec![1.0, -1.0, 2.0, 0.0]);
        assert_eq!(mixer.imbalance(), 0);
    }

    #[test]
    fn prepended_silence_delays_the_channel_it_is_added_to() {
        let mut mixer = StereoMixer::new();
        mixer.push_left(&[1.0, 2.0]);
        mixer.push_right(&[-1.0, -2.0]);
        // The microphone started two frames late: give it two frames of silence.
        mixer.push_front_left(&[0.0, 0.0]);

        let mut out = Vec::new();
        mixer.drain_into(&mut out);
        // The system audio that predates any microphone audio is preserved,
        // paired with silence rather than with the wrong microphone samples.
        assert_eq!(out, vec![0.0, -1.0, 0.0, -2.0]);
        assert_eq!(mixer.imbalance(), 2, "the two real samples still wait");
    }

    #[test]
    fn prepending_keeps_sample_order() {
        let mut mixer = StereoMixer::new();
        mixer.push_left(&[9.0]);
        mixer.push_front_left(&[1.0, 2.0, 3.0]);
        mixer.push_right(&[0.0; 4]);

        let mut out = Vec::new();
        mixer.drain_into(&mut out);
        let left: Vec<f32> = out.iter().step_by(2).copied().collect();
        assert_eq!(left, vec![1.0, 2.0, 3.0, 9.0], "prepend must not reverse");
    }

    #[test]
    fn imbalance_is_signed_towards_whichever_side_leads() {
        let mut mixer = StereoMixer::new();
        mixer.push_right(&[0.0; 5]);
        assert_eq!(mixer.imbalance(), -5, "negative means the system leads");
    }
}
