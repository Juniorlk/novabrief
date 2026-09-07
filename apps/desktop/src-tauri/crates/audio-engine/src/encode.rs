//! Opus encoding into segmented Ogg files, with a manifest.
//!
//! Raw 16 kHz stereo float is 128 kB/s — 461 MB for a one-hour meeting, which
//! is unusable over a Cameroonian upload link. Opus at 32 kbit/s brings the same
//! hour to about 14 MB while staying transparent enough for speech, and every
//! transcription provider accepts it.
//!
//! Audio is written as a sequence of short segments rather than one long file
//! (ADR-05): each finished segment is flushed to disk and hashed, so a crash or
//! a power cut costs at most the segment in progress, and the uploader can send
//! and confirm one piece at a time.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;

use audiopus::coder::Encoder;
use audiopus::{Application, Bitrate, Channels, SampleRate};
use sha2::{Digest, Sha256};

use crate::resample::TARGET_SAMPLE_RATE;

/// Opus frame duration. 20 ms is the codec's sweet spot for speech and what
/// every provider expects.
const FRAME_MS: usize = 20;

/// Samples per channel in one Opus frame at the working rate.
const FRAME_SAMPLES: usize = TARGET_SAMPLE_RATE as usize * FRAME_MS / 1000;

/// Interleaved stereo samples in one frame.
const FRAME_INTERLEAVED: usize = FRAME_SAMPLES * 2;

/// Upper bound for one encoded packet. Opus never exceeds this at our bitrate.
const MAX_PACKET: usize = 4000;

/// Ogg Opus always counts granule positions in 48 kHz samples, whatever the
/// stream's own rate. Writing them in the stream's rate is a spec violation
/// that leaves players disagreeing about the duration.
const GRANULE_RATE: u64 = 48_000;

/// Samples of granule position added by one 20 ms frame.
const FRAME_GRANULE: u64 = GRANULE_RATE * FRAME_MS as u64 / 1000;

/// Encoding and segmenting failures.
#[derive(Debug, thiserror::Error)]
pub enum EncodeError {
    #[error("the Opus encoder could not be created: {0}")]
    EncoderInit(#[source] audiopus::Error),

    #[error("encoding an Opus frame failed: {0}")]
    Encode(#[source] audiopus::Error),

    #[error("writing segment {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("serialising the manifest: {0}")]
    Manifest(#[source] serde_json::Error),
}

/// One written segment, as recorded in the manifest.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct SegmentRecord {
    /// File name, relative to the manifest.
    pub file: String,
    /// Zero-based index in the recording.
    pub index: u32,
    /// Samples per channel encoded into this segment.
    pub samples: u64,
    /// Duration in milliseconds, derived from the sample count.
    pub duration_ms: u64,
    /// Offset of the segment's first sample from the start of the recording.
    pub start_ms: u64,
    /// Size of the segment file in bytes.
    pub bytes: u64,
    /// SHA-256 of the segment file, so the server can verify what it received.
    pub sha256: String,
}

/// The recording's manifest, written next to its segments.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct Manifest {
    /// Manifest schema version, so a newer client is recognisable.
    pub version: u32,
    /// Sample rate of the encoded audio.
    pub sample_rate: u32,
    /// Channel count: always 2, left = microphone, right = system.
    pub channels: u16,
    /// Opus bitrate in bits per second.
    pub bitrate: u32,
    /// Device that provided the microphone.
    pub input_device: String,
    /// Device captured in loopback.
    pub output_device: String,
    /// Measured start skew between the two endpoints, in milliseconds.
    pub start_skew_ms: i64,
    /// Total samples per channel across every segment.
    pub total_samples: u64,
    /// Total duration in milliseconds.
    pub duration_ms: u64,
    /// The segments, in order.
    pub segments: Vec<SegmentRecord>,
}

/// Encodes interleaved stereo samples to Opus, rolling over to a new Ogg
/// segment once the target segment length is reached.
///
/// Samples that do not fill a 20 ms frame are held back until the next call, so
/// a caller can push whatever chunk sizes WASAPI produced without losing audio
/// or padding the timeline with artificial silence.
pub struct SegmentedOpusWriter {
    directory: PathBuf,
    prefix: String,
    encoder: Encoder,
    bitrate: u32,
    segment_samples: u64,

    pending: Vec<f32>,
    packet: Vec<u8>,
    /// Encoder lookahead in 48 kHz samples, written as the Ogg Opus pre-skip.
    pre_skip: u16,
    /// The most recently encoded packet, not yet handed to the muxer.
    ///
    /// Ogg needs to know which packet ends the stream, and that is only known
    /// once the next one fails to arrive, so one packet is always held back.
    held: Option<Vec<u8>>,

    current: Option<OpenSegment>,
    segments: Vec<SegmentRecord>,
    total_samples: u64,
}

struct OpenSegment {
    index: u32,
    path: PathBuf,
    writer: ogg::PacketWriter<'static, BufWriter<File>>,
    hasher: Sha256,
    samples: u64,
    start_samples: u64,
    granule: u64,
    serial: u32,
}

impl std::fmt::Debug for SegmentedOpusWriter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SegmentedOpusWriter")
            .field("directory", &self.directory)
            .field("bitrate", &self.bitrate)
            .field("segments", &self.segments.len())
            .field("total_samples", &self.total_samples)
            .finish_non_exhaustive()
    }
}

impl SegmentedOpusWriter {
    /// Create a writer that fills `directory` with `prefix-NNNN.opus` segments.
    ///
    /// `segment_seconds` is clamped to the 5-10 s window the brief fixed: below
    /// that the per-segment overhead dominates, above it a crash costs too much
    /// audio.
    pub fn new(
        directory: impl Into<PathBuf>,
        prefix: impl Into<String>,
        bitrate: u32,
        segment_seconds: f64,
    ) -> Result<Self, EncodeError> {
        let mut encoder = Encoder::new(SampleRate::Hz16000, Channels::Stereo, Application::Voip)
            .map_err(EncodeError::EncoderInit)?;
        encoder
            .set_bitrate(Bitrate::BitsPerSecond(bitrate as i32))
            .map_err(EncodeError::EncoderInit)?;

        // The encoder delays the signal by its lookahead; declaring it as
        // pre-skip is what lets a decoder drop exactly that much and keep the
        // timeline aligned with the manifest.
        let pre_skip = encoder
            .lookahead()
            .unwrap_or(312)
            .clamp(0, u32::from(u16::MAX)) as u16;

        let seconds = segment_seconds.clamp(5.0, 10.0);
        Ok(Self {
            pre_skip,
            held: None,
            directory: directory.into(),
            prefix: prefix.into(),
            encoder,
            bitrate,
            segment_samples: (seconds * f64::from(TARGET_SAMPLE_RATE)) as u64,
            pending: Vec::with_capacity(FRAME_INTERLEAVED * 4),
            packet: vec![0_u8; MAX_PACKET],
            current: None,
            segments: Vec::new(),
            total_samples: 0,
        })
    }

    /// Segments closed so far.
    #[must_use]
    pub fn segments(&self) -> &[SegmentRecord] {
        &self.segments
    }

    /// Samples per channel encoded so far.
    #[must_use]
    pub const fn total_samples(&self) -> u64 {
        self.total_samples
    }

    /// Encode interleaved stereo samples.
    pub fn write(&mut self, interleaved: &[f32]) -> Result<(), EncodeError> {
        self.pending.extend_from_slice(interleaved);

        while self.pending.len() >= FRAME_INTERLEAVED {
            if self.current.is_none() {
                self.open_segment()?;
            }

            let encoded = {
                let frame = &self.pending[..FRAME_INTERLEAVED];
                self.encoder
                    .encode_float(frame, &mut self.packet)
                    .map_err(EncodeError::Encode)?
            };
            self.pending.drain(..FRAME_INTERLEAVED);

            let packet = self.packet[..encoded].to_vec();
            if let Some(previous) = self.held.replace(packet) {
                self.emit(previous, false)?;
            }
            self.total_samples += FRAME_SAMPLES as u64;

            // A segment is closed on the packet that fills it, and that packet
            // must be the one carrying the end-of-stream flag, so the held
            // packet is released here rather than waiting for the next frame.
            let full = self.current.as_ref().is_some_and(|segment| {
                segment.samples + FRAME_SAMPLES as u64 >= self.segment_samples
            });
            if full {
                if let Some(last) = self.held.take() {
                    self.emit(last, true)?;
                }
                self.close_segment()?;
            }
        }
        Ok(())
    }

    /// Flush the tail and write the manifest.
    ///
    /// Samples left over from an incomplete frame are padded with silence: Opus
    /// only encodes whole frames, and dropping them would silently shorten the
    /// recording.
    pub fn finish(
        mut self,
        input_device: &str,
        output_device: &str,
        start_skew_ms: i64,
    ) -> Result<Manifest, EncodeError> {
        if !self.pending.is_empty() {
            self.pending.resize(FRAME_INTERLEAVED, 0.0);
            if self.current.is_none() {
                self.open_segment()?;
            }
            let encoded = {
                let frame = &self.pending[..FRAME_INTERLEAVED];
                self.encoder
                    .encode_float(frame, &mut self.packet)
                    .map_err(EncodeError::Encode)?
            };
            self.pending.clear();
            let packet = self.packet[..encoded].to_vec();
            if let Some(previous) = self.held.replace(packet) {
                self.emit(previous, false)?;
            }
            self.total_samples += FRAME_SAMPLES as u64;
        }

        if let Some(last) = self.held.take() {
            self.emit(last, true)?;
        }
        if self.current.is_some() {
            self.close_segment()?;
        }

        let manifest = Manifest {
            version: 1,
            sample_rate: TARGET_SAMPLE_RATE,
            channels: 2,
            bitrate: self.bitrate,
            input_device: input_device.to_owned(),
            output_device: output_device.to_owned(),
            start_skew_ms,
            total_samples: self.total_samples,
            duration_ms: samples_to_ms(self.total_samples),
            segments: self.segments.clone(),
        };

        let path = self
            .directory
            .join(format!("{}-manifest.json", self.prefix));
        let json = serde_json::to_string_pretty(&manifest).map_err(EncodeError::Manifest)?;
        std::fs::write(&path, json).map_err(|source| EncodeError::Io { path, source })?;
        Ok(manifest)
    }

    fn open_segment(&mut self) -> Result<(), EncodeError> {
        let index = self.segments.len() as u32;
        let path = self
            .directory
            .join(format!("{}-{index:04}.opus", self.prefix));

        std::fs::create_dir_all(&self.directory).map_err(|source| EncodeError::Io {
            path: self.directory.clone(),
            source,
        })?;
        let file = File::create(&path).map_err(|source| EncodeError::Io {
            path: path.clone(),
            source,
        })?;

        // Each segment is a standalone Ogg stream so it can be decoded on its
        // own; a distinct serial keeps them from being mistaken for one stream.
        let serial = 0x4E42_0000_u32.wrapping_add(index);
        let mut segment = OpenSegment {
            index,
            path,
            writer: ogg::PacketWriter::new(BufWriter::new(file)),
            hasher: Sha256::new(),
            samples: 0,
            start_samples: self.total_samples,
            granule: 0,
            serial,
        };
        write_opus_headers(&mut segment, self.pre_skip)?;
        self.current = Some(segment);
        Ok(())
    }

    /// Hand one encoded packet to the muxer.
    ///
    /// `last` marks the end of the logical Ogg stream. Ogg needs that flag on a
    /// real packet: a zero-length packet is not a valid Opus packet, so an
    /// empty end marker makes the whole stream undecodable.
    fn emit(&mut self, data: Vec<u8>, last: bool) -> Result<(), EncodeError> {
        if self.current.is_none() {
            self.open_segment()?;
        }
        let Some(segment) = self.current.as_mut() else {
            return Ok(());
        };
        segment.granule += FRAME_GRANULE;
        segment.samples += FRAME_SAMPLES as u64;

        let info = if last {
            ogg::PacketWriteEndInfo::EndStream
        } else {
            ogg::PacketWriteEndInfo::NormalPacket
        };
        segment
            .writer
            .write_packet(data, segment.serial, info, segment.granule)
            .map_err(|source| EncodeError::Io {
                path: segment.path.clone(),
                source,
            })
    }

    fn close_segment(&mut self) -> Result<(), EncodeError> {
        let Some(mut segment) = self.current.take() else {
            return Ok(());
        };

        let mut inner = segment.writer.into_inner();
        inner.flush().map_err(|source| EncodeError::Io {
            path: segment.path.clone(),
            source,
        })?;
        drop(inner);

        // Hash the file as it landed on disk rather than the bytes we meant to
        // write: the manifest must describe what will actually be uploaded.
        let bytes = std::fs::read(&segment.path).map_err(|source| EncodeError::Io {
            path: segment.path.clone(),
            source,
        })?;
        segment.hasher.update(&bytes);
        let digest = segment.hasher.finalize();

        self.segments.push(SegmentRecord {
            file: segment
                .path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default(),
            index: segment.index,
            samples: segment.samples,
            duration_ms: samples_to_ms(segment.samples),
            start_ms: samples_to_ms(segment.start_samples),
            bytes: bytes.len() as u64,
            sha256: format!("{digest:x}"),
        });
        Ok(())
    }
}

/// Write the two mandatory Ogg Opus headers, `OpusHead` and `OpusTags`.
fn write_opus_headers(segment: &mut OpenSegment, pre_skip: u16) -> Result<(), EncodeError> {
    let mut head = Vec::with_capacity(19);
    head.extend_from_slice(b"OpusHead");
    head.push(1); // version
    head.push(2); // channel count
    head.extend_from_slice(&pre_skip.to_le_bytes()); // pre-skip
    head.extend_from_slice(&TARGET_SAMPLE_RATE.to_le_bytes()); // original rate
    head.extend_from_slice(&0_i16.to_le_bytes()); // output gain
    head.push(0); // channel mapping family 0

    let vendor = b"NovaBrief";
    let mut tags = Vec::with_capacity(32);
    tags.extend_from_slice(b"OpusTags");
    tags.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
    tags.extend_from_slice(vendor);
    tags.extend_from_slice(&0_u32.to_le_bytes()); // no user comments

    for (packet, info) in [
        (head, ogg::PacketWriteEndInfo::EndPage),
        (tags, ogg::PacketWriteEndInfo::EndPage),
    ] {
        segment
            .writer
            .write_packet(packet, segment.serial, info, 0)
            .map_err(|source| EncodeError::Io {
                path: segment.path.clone(),
                source,
            })?;
    }
    Ok(())
}

/// Convert a per-channel sample count at the working rate to milliseconds.
#[must_use]
pub const fn samples_to_ms(samples: u64) -> u64 {
    samples * 1000 / TARGET_SAMPLE_RATE as u64
}

#[cfg(test)]
mod tests {
    use super::{samples_to_ms, SegmentedOpusWriter, FRAME_INTERLEAVED, FRAME_SAMPLES};
    use crate::resample::TARGET_SAMPLE_RATE;

    fn tone(seconds: f64) -> Vec<f32> {
        let frames = (seconds * f64::from(TARGET_SAMPLE_RATE)) as usize;
        (0..frames)
            .flat_map(|n| {
                let value = (n as f32 * 2.0 * std::f32::consts::PI * 440.0 / 16_000.0).sin() * 0.5;
                [value, value * 0.5]
            })
            .collect()
    }

    #[test]
    fn a_frame_is_twenty_milliseconds() {
        assert_eq!(FRAME_SAMPLES, 320);
        assert_eq!(FRAME_INTERLEAVED, 640);
        assert_eq!(samples_to_ms(FRAME_SAMPLES as u64), 20);
    }

    #[test]
    fn encoding_is_far_smaller_than_raw_audio() {
        let dir = std::env::temp_dir().join(format!("nb-opus-size-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        let seconds = 12.0;
        writer.write(&tone(seconds)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");

        let encoded: u64 = manifest.segments.iter().map(|s| s.bytes).sum();
        let raw = (seconds * f64::from(TARGET_SAMPLE_RATE) * 2.0 * 4.0) as u64;
        assert!(
            encoded * 10 < raw,
            "Opus should be at least ten times smaller: {encoded} vs {raw}"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn audio_is_split_into_segments_of_the_requested_length() {
        let dir = std::env::temp_dir().join(format!("nb-opus-seg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        writer.write(&tone(12.0)).expect("write");
        let manifest = writer.finish("mic", "speakers", -800).expect("finish");

        assert_eq!(manifest.segments.len(), 3, "12 s at 5 s per segment");
        assert_eq!(manifest.segments[0].start_ms, 0);
        assert_eq!(manifest.segments[1].start_ms, 5_000);
        assert_eq!(manifest.start_skew_ms, -800);

        // Every segment exists, is non-empty and carries a real digest.
        for segment in &manifest.segments {
            let path = dir.join(&segment.file);
            let bytes = std::fs::read(&path).expect("segment file");
            assert_eq!(bytes.len() as u64, segment.bytes);
            assert_eq!(segment.sha256.len(), 64);
            assert!(bytes.starts_with(b"OggS"), "segment must be an Ogg stream");
        }

        // The manifest lands next to the segments.
        assert!(dir.join("test-manifest.json").is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn segment_length_is_clamped_to_the_agreed_window() {
        let dir = std::env::temp_dir().join(format!("nb-opus-clamp-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        // One second is below the 5 s floor, so segments must still be 5 s.
        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 1.0).expect("writer");
        writer.write(&tone(11.0)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");
        assert_eq!(manifest.segments.len(), 3, "clamped to 5 s, not 1 s");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_short_tail_is_kept_rather_than_dropped() {
        let dir = std::env::temp_dir().join(format!("nb-opus-tail-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        // Half a frame: too short to encode on its own.
        writer.write(&tone(0.01)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");

        assert_eq!(manifest.segments.len(), 1);
        assert!(manifest.total_samples >= FRAME_SAMPLES as u64);

        let _ = std::fs::remove_dir_all(&dir);
    }
}
