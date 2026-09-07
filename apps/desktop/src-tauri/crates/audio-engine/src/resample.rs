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

/// Resamples one mono stream to [`TARGET_SAMPLE_RATE`].
///
/// Input arrives in whatever sizes WASAPI hands out, so the incoming samples are
/// buffered and consumed in fixed chunks; whatever does not fill a chunk waits
/// for the next call rather than being dropped.
pub struct MonoResampler {
    inner: Option<SincFixedIn<f32>>,
    pending: Vec<f32>,
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

        while self.pending.len() >= CHUNK {
            let chunk: Vec<f32> = self.pending.drain(..CHUNK).collect();
            self.scratch_in.clear();
            self.scratch_in.push(chunk);

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
    use super::{MonoResampler, StereoMixer, TARGET_SAMPLE_RATE};

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
    fn imbalance_is_signed_towards_whichever_side_leads() {
        let mut mixer = StereoMixer::new();
        mixer.push_right(&[0.0; 5]);
        assert_eq!(mixer.imbalance(), -5, "negative means the system leads");
    }
}
