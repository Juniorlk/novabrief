//! Reading and converting the WASAPI mix format.
//!
//! Windows negotiates the shared-mode mix format itself, so the engine reads it
//! with `GetMixFormat` and adapts. Assuming 48 kHz stereo float is the classic
//! way to produce silence on somebody else's laptop: mix formats seen in the
//! wild range over 44.1/48/96 kHz, 1 to 8 channels, and both integer and float
//! samples.

use crate::error::{CaptureError, Endpoint, Result};

/// Sample encodings we know how to turn into `f32`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    /// 32-bit IEEE float, the usual shared-mode mix format.
    F32,
    /// 16-bit signed integer.
    I16,
    /// 24-bit signed integer packed in three bytes.
    I24,
    /// 32-bit signed integer.
    I32,
}

impl SampleFormat {
    /// Bytes occupied by one sample of one channel.
    const fn bytes(self) -> usize {
        match self {
            Self::F32 | Self::I32 => 4,
            Self::I24 => 3,
            Self::I16 => 2,
        }
    }
}

/// The shape of an audio stream as WASAPI hands it to us.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamFormat {
    pub sample_rate: u32,
    pub channels: u16,
    pub sample_format: SampleFormat,
}

impl StreamFormat {
    /// Bytes per frame, i.e. one sample for every channel.
    #[must_use]
    pub const fn bytes_per_frame(&self) -> usize {
        self.sample_format.bytes() * self.channels as usize
    }

    /// Decode `frames` interleaved frames of raw device bytes into interleaved
    /// `f32` samples in `out`, normalised to [-1.0, 1.0].
    ///
    /// `bytes` must hold at least `frames * self.bytes_per_frame()` bytes; extra
    /// trailing bytes are ignored.
    pub fn decode(&self, bytes: &[u8], frames: usize, out: &mut Vec<f32>) {
        let samples = frames * self.channels as usize;
        let width = self.sample_format.bytes();
        out.clear();
        out.reserve(samples);

        for i in 0..samples {
            let start = i * width;
            let Some(chunk) = bytes.get(start..start + width) else {
                break;
            };
            out.push(match self.sample_format {
                SampleFormat::F32 => f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]),
                SampleFormat::I16 => f32::from(i16::from_le_bytes([chunk[0], chunk[1]])) / 32_768.0,
                SampleFormat::I24 => {
                    // Sign-extend the three packed bytes into an i32.
                    let raw = i32::from_le_bytes([0, chunk[0], chunk[1], chunk[2]]) >> 8;
                    raw as f32 / 8_388_608.0
                }
                SampleFormat::I32 => {
                    let raw = i32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
                    raw as f32 / 2_147_483_648.0
                }
            });
        }
    }
}

/// Average the channels of an interleaved buffer into a mono buffer.
///
/// Averaging rather than taking the first channel keeps a voice that is panned
/// or that only appears on the right channel.
pub fn downmix_to_mono(interleaved: &[f32], channels: u16, out: &mut Vec<f32>) {
    let channels = channels.max(1) as usize;
    out.clear();
    out.reserve(interleaved.len() / channels);
    for frame in interleaved.chunks_exact(channels) {
        let sum: f32 = frame.iter().sum();
        out.push(sum / channels as f32);
    }
}

/// Peak absolute amplitude of a buffer, used to drive the text VU meters.
#[must_use]
pub fn peak(samples: &[f32]) -> f32 {
    samples.iter().fold(0.0_f32, |acc, s| acc.max(s.abs()))
}

#[cfg(windows)]
mod win {
    use super::{CaptureError, Endpoint, Result, SampleFormat, StreamFormat};
    use windows::core::GUID;
    use windows::Win32::Media::Audio::{WAVEFORMATEX, WAVEFORMATEXTENSIBLE};

    // Declared locally as u16 to match WAVEFORMATEX::wFormatTag. The same values
    // exist across several windows-rs modules with different integer widths,
    // and casting at every comparison reads worse than naming them once.
    const WAVE_FORMAT_PCM: u16 = 0x0001;
    const WAVE_FORMAT_IEEE_FLOAT: u16 = 0x0003;
    const WAVE_FORMAT_EXTENSIBLE: u16 = 0xFFFE;
    const SUBTYPE_PCM: GUID = GUID::from_u128(0x0000_0001_0000_0010_8000_00aa_0038_9b71);
    const SUBTYPE_IEEE_FLOAT: GUID = GUID::from_u128(0x0000_0003_0000_0010_8000_00aa_0038_9b71);

    /// Read a `WAVEFORMATEX` (possibly a `WAVEFORMATEXTENSIBLE`) into our own
    /// description.
    ///
    /// # Safety
    /// `ptr` must point at a valid `WAVEFORMATEX` owned by the caller, and when
    /// its `wFormatTag` is `WAVE_FORMAT_EXTENSIBLE` the allocation must actually
    /// be a `WAVEFORMATEXTENSIBLE`, as WASAPI guarantees for `GetMixFormat`.
    pub unsafe fn stream_format_from_waveformatex(
        ptr: *const WAVEFORMATEX,
        endpoint: Endpoint,
    ) -> Result<StreamFormat> {
        let wfx = unsafe { &*ptr };
        let bits = wfx.wBitsPerSample;
        let tag = wfx.wFormatTag;

        // For WAVE_FORMAT_EXTENSIBLE the real encoding lives in the SubFormat
        // GUID; the tag alone only says "look further".
        let effective_tag = if tag == WAVE_FORMAT_EXTENSIBLE {
            let ext = unsafe { &*ptr.cast::<WAVEFORMATEXTENSIBLE>() };
            match ext.SubFormat {
                SUBTYPE_IEEE_FLOAT => WAVE_FORMAT_IEEE_FLOAT,
                SUBTYPE_PCM => WAVE_FORMAT_PCM,
                _ => 0,
            }
        } else {
            tag
        };

        let sample_format = match (effective_tag, bits) {
            (WAVE_FORMAT_IEEE_FLOAT, 32) => SampleFormat::F32,
            (WAVE_FORMAT_PCM, 16) => SampleFormat::I16,
            (WAVE_FORMAT_PCM, 24) => SampleFormat::I24,
            (WAVE_FORMAT_PCM, 32) => SampleFormat::I32,
            _ => {
                return Err(CaptureError::UnsupportedFormat {
                    endpoint,
                    bits,
                    channels: wfx.nChannels,
                    sample_rate: wfx.nSamplesPerSec,
                    tag,
                });
            }
        };

        Ok(StreamFormat {
            sample_rate: wfx.nSamplesPerSec,
            channels: wfx.nChannels,
            sample_format,
        })
    }
}

#[cfg(windows)]
pub use win::stream_format_from_waveformatex;

#[cfg(test)]
mod tests {
    use super::{downmix_to_mono, peak, SampleFormat, StreamFormat};

    fn fmt(sample_format: SampleFormat, channels: u16) -> StreamFormat {
        StreamFormat {
            sample_rate: 48_000,
            channels,
            sample_format,
        }
    }

    #[test]
    fn decodes_f32_samples_unchanged() {
        let format = fmt(SampleFormat::F32, 2);
        let mut bytes = Vec::new();
        for value in [0.0_f32, 1.0, -1.0, 0.5] {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        let mut out = Vec::new();
        format.decode(&bytes, 2, &mut out);
        assert_eq!(out, vec![0.0, 1.0, -1.0, 0.5]);
    }

    #[test]
    fn decodes_i16_full_scale_to_unit_range() {
        let format = fmt(SampleFormat::I16, 1);
        let mut bytes = Vec::new();
        for value in [0_i16, i16::MAX, i16::MIN] {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        let mut out = Vec::new();
        format.decode(&bytes, 3, &mut out);
        assert_eq!(out[0], 0.0);
        assert!((out[1] - 1.0).abs() < 1e-4, "got {}", out[1]);
        assert!((out[2] + 1.0).abs() < 1e-6, "got {}", out[2]);
    }

    #[test]
    fn decodes_i24_preserving_sign() {
        let format = fmt(SampleFormat::I24, 1);
        // -1 and +1 in 24-bit two's complement, little endian.
        let bytes = [0xFF, 0xFF, 0xFF, 0x01, 0x00, 0x00];
        let mut out = Vec::new();
        format.decode(&bytes, 2, &mut out);
        assert!(
            out[0] < 0.0,
            "0xFFFFFF should decode negative, got {}",
            out[0]
        );
        assert!(
            out[1] > 0.0,
            "0x000001 should decode positive, got {}",
            out[1]
        );
    }

    #[test]
    fn decode_stops_at_a_truncated_frame_instead_of_panicking() {
        let format = fmt(SampleFormat::F32, 2);
        let bytes = [0_u8; 6]; // one and a half samples
        let mut out = Vec::new();
        format.decode(&bytes, 2, &mut out);
        assert_eq!(out.len(), 1);
    }

    #[test]
    fn bytes_per_frame_accounts_for_every_channel() {
        assert_eq!(fmt(SampleFormat::F32, 2).bytes_per_frame(), 8);
        assert_eq!(fmt(SampleFormat::I16, 6).bytes_per_frame(), 12);
    }

    #[test]
    fn downmix_averages_channels() {
        let mut out = Vec::new();
        downmix_to_mono(&[1.0, 0.0, 0.5, 0.5], 2, &mut out);
        assert_eq!(out, vec![0.5, 0.5]);
    }

    #[test]
    fn downmix_keeps_a_voice_present_on_one_side_only() {
        let mut out = Vec::new();
        downmix_to_mono(&[0.0, 0.8], 2, &mut out);
        assert!(out[0] > 0.0, "a right-only voice must survive the downmix");
    }

    #[test]
    fn peak_reports_the_largest_absolute_value() {
        assert_eq!(peak(&[0.1, -0.7, 0.3]), 0.7);
        assert_eq!(peak(&[]), 0.0);
    }
}
