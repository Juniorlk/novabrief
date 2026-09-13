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
//!
//! The segments are **pieces of one Ogg stream**, not a series of little
//! files. Put back together end to end they are the recording, byte for byte,
//! which is what the server is sent: one object at one URL. Cutting the stream
//! into standalone files instead would make the joined result a chained Ogg,
//! and a decoder trims the declared pre-skip at the start of every link while
//! the Opus encoder underneath pays that delay only once - so an hour of
//! meeting, 720 links, would lose several seconds of speech in small bites and
//! shift every timestamp after each one.

use std::fs::File;
use std::io::Write;
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

/// Dead prefix tolerated before the pending buffer is compacted.
///
/// Small enough that an idle encoder does not sit on stale samples, large
/// enough that the steady state - a few frames in flight - never copies at all.
const COMPACT_THRESHOLD: usize = FRAME_INTERLEAVED * 8;

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
pub struct SegmentedOpusWriter<S: SegmentSink = FileSegmentSink> {
    sink: S,
    encoder: Encoder,
    bitrate: u32,
    segment_samples: u64,

    pending: Vec<f32>,
    /// How much of `pending` has already been encoded.
    ///
    /// Consuming the front of a `Vec` with `drain(..n)` shifts everything after
    /// it, so a buffer that is behind costs O(n) *per frame* and O(n squared)
    /// over a backlog. That is a feedback loop rather than a constant penalty:
    /// the further behind the encoder falls, the more each frame costs, and the
    /// further behind it falls. Over an hour it took memory to 668 MB and cost
    /// 10.25 % of the audio.
    ///
    /// A cursor instead. Nothing moves while frames are consumed; the tail is
    /// compacted only once the dead prefix is worth more than the copy, which
    /// makes the amortised cost per sample constant.
    head: usize,
    /// Samples physically copied by compaction.
    ///
    /// Kept because this is the quantity that regressed: with `drain` it grew
    /// with the square of the backlog, and nothing in the output would have
    /// shown it. A counter makes the invariant testable instead of a matter of
    /// trust - see `compaction_stays_linear_under_backlog`.
    moved: u64,
    packet: Vec<u8>,
    /// Encoder lookahead in 48 kHz samples, written as the Ogg Opus pre-skip.
    pre_skip: u16,
    /// The most recently encoded packet, not yet handed to the muxer.
    ///
    /// Ogg needs to know which packet ends the stream, and that is only known
    /// once the next one fails to arrive, so one packet is always held back.
    held: Option<Vec<u8>>,

    /// The one Ogg stream the whole recording is written into.
    ///
    /// Kept across segments so page sequence numbers, the serial and the
    /// granule positions run unbroken through the file the segments make when
    /// they are put back together. A writer per segment would restart the page
    /// counter at zero in the middle of the stream.
    stream: ogg::PacketWriter<'static, Vec<u8>>,
    /// Granule position of the last packet handed to the muxer, in 48 kHz
    /// samples, counted from the start of the recording.
    granule: u64,

    current: Option<OpenSegment>,
    segments: Vec<SegmentRecord>,
    total_samples: u64,
}

/// What a packet is at the end of.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Boundary {
    /// An ordinary packet, in the middle of a page.
    Packet,
    /// The last packet of a segment, so the muxer flushes its page there.
    ///
    /// Without it the muxer would flush when a page fills instead, and a
    /// segment would be stored holding neither more nor less than whatever
    /// had happened to be written by then - while its manifest entry claimed
    /// five seconds. A crash would then cost however long ago the last flush
    /// was, not the five seconds EF-17 promises.
    Segment,
    /// The last packet of the recording.
    Stream,
}

/// Where finished segments go.
///
/// The encoder used to create the files itself, which left nowhere to put
/// encryption. EF-17 requires that plaintext audio never reach the disk on the
/// desktop, so the destination becomes the caller's decision: a file for the
/// measurement tool, the encrypted vault for the product.
///
/// A whole segment at a time rather than a stream of bytes. Five seconds of
/// Opus at 32 kbit/s is about 20 KB, so buffering one costs nothing, and
/// AES-GCM needs the whole thing anyway - it authenticates what it encrypts,
/// and cannot do that a chunk at a time.
pub trait SegmentSink {
    /// Take one finished segment, and answer with the name it was stored under.
    ///
    /// The name goes into the manifest, so it is whatever the *sink* calls it -
    /// a file name on disk, an identifier in the vault - rather than something
    /// the encoder invents and hopes the sink agrees with.
    ///
    /// # Errors
    ///
    /// Whatever storing it failed with.
    fn accept(&mut self, index: u32, bytes: &[u8], duration_ms: u64)
        -> Result<String, EncodeError>;

    /// Persist the manifest, if this sink keeps one of its own.
    ///
    /// Does nothing by default. The file sink writes the JSON next to its
    /// segments; the vault already maintains a manifest with more in it - the
    /// sealed key, the recording state - and a second one beside it would be a
    /// second thing to keep in agreement.
    ///
    /// # Errors
    ///
    /// Whatever storing it failed with.
    fn accept_manifest(&mut self, _manifest: &Manifest) -> Result<(), EncodeError> {
        Ok(())
    }
}

/// Writes each segment as `prefix-NNNN.opus` in a directory.
#[derive(Debug, Clone)]
pub struct FileSegmentSink {
    directory: PathBuf,
    prefix: String,
}

impl FileSegmentSink {
    /// A sink that fills `directory`.
    #[must_use]
    pub fn new(directory: impl Into<PathBuf>, prefix: impl Into<String>) -> Self {
        Self {
            directory: directory.into(),
            prefix: prefix.into(),
        }
    }
}

impl SegmentSink for FileSegmentSink {
    fn accept(
        &mut self,
        index: u32,
        bytes: &[u8],
        _duration_ms: u64,
    ) -> Result<String, EncodeError> {
        std::fs::create_dir_all(&self.directory).map_err(|source| EncodeError::Io {
            path: self.directory.clone(),
            source,
        })?;

        let name = format!("{}-{index:04}.opus", self.prefix);
        let path = self.directory.join(&name);
        let write = || -> std::io::Result<()> {
            let mut file = File::create(&path)?;
            file.write_all(bytes)?;
            // Flushed to the platter before the segment is reported as stored.
            // The vault relies on the same ordering for EF-17, and a sink that
            // reported a segment it had only handed to the page cache would
            // make that promise untrue for the file path too.
            file.sync_all()
        };
        write().map_err(|source| EncodeError::Io { path, source })?;
        Ok(name)
    }

    fn accept_manifest(&mut self, manifest: &Manifest) -> Result<(), EncodeError> {
        let path = self
            .directory
            .join(format!("{}-manifest.json", self.prefix));
        let json = serde_json::to_string_pretty(manifest).map_err(EncodeError::Manifest)?;
        std::fs::write(&path, json).map_err(|source| EncodeError::Io { path, source })
    }
}

/// The Ogg serial number every page of a recording carries.
///
/// Fixed rather than random. A serial exists to tell interleaved logical
/// streams apart inside one Ogg file, and a NovaBrief recording has exactly
/// one; a constant makes a segment byte-identical from one run to the next,
/// which is what lets a test compare two encodings at all.
const STREAM_SERIAL: u32 = 0x4E42_0001;

/// How many 48 kHz samples one sample at the working rate is worth.
///
/// Ogg Opus counts pre-skip and granule positions in 48 kHz samples whatever
/// the stream's own rate, so every figure written into the container has to be
/// scaled by this. Writing the encoder's raw lookahead instead would declare a
/// third of the real delay and leave a few milliseconds of encoder lead-in at
/// the front of every recording.
const GRANULE_PER_SAMPLE: u32 = GRANULE_RATE as u32 / TARGET_SAMPLE_RATE;

/// Where one segment starts and how much of the recording it holds.
///
/// It has no Ogg writer of its own. The whole recording is **one** logical Ogg
/// stream, cut at page boundaries: concatenating the segments in order gives
/// back a byte-exact, valid Ogg Opus file, which is what the server is sent.
///
/// The alternative - a standalone stream per segment - produces a chained file
/// instead, and chaining is not free here. A decoder trims the declared
/// pre-skip at the start of *every* link, while the Opus encoder underneath
/// runs continuously and only ever pays that delay once. An hour of meeting is
/// 720 links, so the chained file would lose several seconds of real audio in
/// small bites and shift every timestamp after each one.
struct OpenSegment {
    index: u32,
    samples: u64,
    start_samples: u64,
}

impl<S: SegmentSink> std::fmt::Debug for SegmentedOpusWriter<S> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SegmentedOpusWriter")
            .field("bitrate", &self.bitrate)
            .field("segments", &self.segments.len())
            .field("total_samples", &self.total_samples)
            .finish_non_exhaustive()
    }
}

impl SegmentedOpusWriter<FileSegmentSink> {
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
        Self::with_sink(
            FileSegmentSink::new(directory, prefix),
            bitrate,
            segment_seconds,
        )
    }
}

impl<S: SegmentSink> SegmentedOpusWriter<S> {
    /// Create a writer that hands each finished segment to `sink`.
    ///
    /// # Errors
    ///
    /// [`EncodeError::EncoderInit`] if Opus refuses the configuration.
    pub fn with_sink(sink: S, bitrate: u32, segment_seconds: f64) -> Result<Self, EncodeError> {
        let mut encoder = Encoder::new(SampleRate::Hz16000, Channels::Stereo, Application::Voip)
            .map_err(EncodeError::EncoderInit)?;
        encoder
            .set_bitrate(Bitrate::BitsPerSecond(bitrate as i32))
            .map_err(EncodeError::EncoderInit)?;

        // The encoder delays the signal by its lookahead; declaring it as
        // pre-skip is what lets a decoder drop exactly that much and keep the
        // timeline aligned with the manifest.
        //
        // Scaled to 48 kHz, which is the unit the container counts in.
        // `lookahead` answers in samples at the rate the encoder was created
        // with - 16 kHz here - and writing that figure unscaled declared a
        // third of the real delay.
        let pre_skip = encoder
            .lookahead()
            .unwrap_or(312 / GRANULE_PER_SAMPLE)
            .saturating_mul(GRANULE_PER_SAMPLE)
            .min(u32::from(u16::MAX)) as u16;

        let seconds = segment_seconds.clamp(5.0, 10.0);
        Ok(Self {
            pre_skip,
            held: None,
            sink,
            encoder,
            bitrate,
            segment_samples: (seconds * f64::from(TARGET_SAMPLE_RATE)) as u64,
            pending: Vec::with_capacity(FRAME_INTERLEAVED * 4),
            head: 0,
            moved: 0,
            packet: vec![0_u8; MAX_PACKET],
            // Into memory. Five seconds is about 20 KB, and the sink needs the
            // whole segment anyway.
            stream: ogg::PacketWriter::new(Vec::new()),
            granule: 0,
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

        while self.buffered() >= FRAME_INTERLEAVED {
            if self.current.is_none() {
                self.open_segment()?;
            }

            let encoded = {
                let frame = &self.pending[self.head..self.head + FRAME_INTERLEAVED];
                self.encoder
                    .encode_float(frame, &mut self.packet)
                    .map_err(EncodeError::Encode)?
            };
            self.head += FRAME_INTERLEAVED;

            let packet = self.packet[..encoded].to_vec();
            if let Some(previous) = self.held.replace(packet) {
                self.emit(previous, Boundary::Packet)?;
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
                    self.emit(last, Boundary::Segment)?;
                }
                self.close_segment()?;
            }
        }
        self.compact();
        Ok(())
    }

    /// Samples still waiting to be encoded.
    ///
    /// Public because criterion 2 of the brief is about memory, and a bound
    /// nobody can read is a bound nobody checks.
    #[must_use]
    pub fn buffered(&self) -> usize {
        self.pending.len() - self.head
    }

    /// Drop the encoded prefix, but only when it has grown worth the copy.
    ///
    /// Compacting on every call would restore exactly the per-frame O(n) this
    /// cursor exists to avoid. Waiting until the dead prefix is at least half
    /// the buffer means each sample is moved at most once per doubling, so the
    /// amortised cost is constant however far behind the encoder gets.
    fn compact(&mut self) {
        if self.head == 0 {
            return;
        }
        if self.head < self.pending.len() - self.head && self.head < COMPACT_THRESHOLD {
            return;
        }
        self.moved += (self.pending.len() - self.head) as u64;
        self.pending.copy_within(self.head.., 0);
        self.pending.truncate(self.pending.len() - self.head);
        self.head = 0;
    }

    /// Samples moved by compaction so far.
    #[must_use]
    pub const fn samples_moved(&self) -> u64 {
        self.moved
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
        if self.buffered() > 0 {
            // The cursor is dropped first: what is left to flush is the live
            // region, and padding has to be measured from there rather than
            // from a buffer that still carries everything already encoded.
            self.pending.copy_within(self.head.., 0);
            self.pending.truncate(self.buffered());
            self.head = 0;

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
            self.head = 0;
            let packet = self.packet[..encoded].to_vec();
            if let Some(previous) = self.held.replace(packet) {
                self.emit(previous, Boundary::Packet)?;
            }
            self.total_samples += FRAME_SAMPLES as u64;
        }

        if let Some(last) = self.held.take() {
            self.emit(last, Boundary::Stream)?;
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

        self.sink.accept_manifest(&manifest)?;
        Ok(manifest)
    }

    fn open_segment(&mut self) -> Result<(), EncodeError> {
        let index = self.segments.len() as u32;

        // The two mandatory headers open the stream, once, at the front of the
        // first segment - not once per segment. They describe the whole
        // recording, and a decoder that met them again mid-file would read the
        // rest as a second, unrelated stream.
        if index == 0 {
            self.write_opus_headers()?;
        }

        self.current = Some(OpenSegment {
            index,
            samples: 0,
            start_samples: self.total_samples,
        });
        Ok(())
    }

    /// Write the two mandatory Ogg Opus headers, `OpusHead` and `OpusTags`.
    fn write_opus_headers(&mut self) -> Result<(), EncodeError> {
        let mut head = Vec::with_capacity(19);
        head.extend_from_slice(b"OpusHead");
        head.push(1); // version
        head.push(2); // channel count
        head.extend_from_slice(&self.pre_skip.to_le_bytes()); // pre-skip
        head.extend_from_slice(&TARGET_SAMPLE_RATE.to_le_bytes()); // original rate
        head.extend_from_slice(&0_i16.to_le_bytes()); // output gain
        head.push(0); // channel mapping family 0

        let vendor = b"NovaBrief";
        let mut tags = Vec::with_capacity(32);
        tags.extend_from_slice(b"OpusTags");
        tags.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
        tags.extend_from_slice(vendor);
        tags.extend_from_slice(&0_u32.to_le_bytes()); // no user comments

        // Each on a page of its own, as RFC 7845 requires.
        for packet in [head, tags] {
            self.stream
                .write_packet(packet, STREAM_SERIAL, ogg::PacketWriteEndInfo::EndPage, 0)
                .map_err(|source| EncodeError::Io {
                    path: PathBuf::from("opus headers"),
                    source,
                })?;
        }
        Ok(())
    }

    /// Hand one encoded packet to the muxer.
    ///
    /// [`Boundary::Stream`] marks the end of the logical Ogg stream. Ogg needs
    /// that flag on a real packet: a zero-length packet is not a valid Opus
    /// packet, so an empty end marker makes the whole stream undecodable.
    fn emit(&mut self, data: Vec<u8>, boundary: Boundary) -> Result<(), EncodeError> {
        if self.current.is_none() {
            self.open_segment()?;
        }
        let Some(segment) = self.current.as_mut() else {
            return Ok(());
        };
        let index = segment.index;
        segment.samples += FRAME_SAMPLES as u64;
        self.granule += FRAME_GRANULE;

        let info = match boundary {
            Boundary::Packet => ogg::PacketWriteEndInfo::NormalPacket,
            Boundary::Segment => ogg::PacketWriteEndInfo::EndPage,
            Boundary::Stream => ogg::PacketWriteEndInfo::EndStream,
        };
        self.stream
            .write_packet(data, STREAM_SERIAL, info, self.granule)
            .map_err(|source| EncodeError::Io {
                path: PathBuf::from(format!("segment {index}")),
                source,
            })
    }

    fn close_segment(&mut self) -> Result<(), EncodeError> {
        let Some(segment) = self.current.take() else {
            return Ok(());
        };

        // Everything the muxer has produced since the previous segment was
        // closed, which is a whole number of pages: the last packet of a
        // segment always ends its page.
        let bytes = std::mem::take(self.stream.inner_mut());

        // Of the bytes the encoder produced, which are exactly what the sink
        // stores and what will later be uploaded. This used to read the file
        // back to hash what had landed on disk - a useful check when the
        // encoder owned the file, and a meaningless one now that it does not:
        // each sink is responsible for its own durability, and the vault hashes
        // its ciphertext separately.
        let digest = Sha256::digest(&bytes);
        let file = self
            .sink
            .accept(segment.index, &bytes, samples_to_ms(segment.samples))?;

        self.segments.push(SegmentRecord {
            file,
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

/// Convert a per-channel sample count at the working rate to milliseconds.
#[must_use]
pub const fn samples_to_ms(samples: u64) -> u64 {
    samples * 1000 / TARGET_SAMPLE_RATE as u64
}

#[cfg(test)]
mod tests {

    /// A directory of this test's own.
    ///
    /// The counter is not decoration. Tests share a process, so the pid does
    /// not separate them, and picking a name by hand already collided once:
    /// `scratch("tail")` landed on the directory
    /// `a_short_tail_is_kept_rather_than_dropped` had been using for months.
    /// The two deleted each other's segments and the suite failed roughly one
    /// run in three, in whichever test lost the race - which is the worst kind
    /// of red, because it accuses innocent code.
    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-opus-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    /// D1: the defect that cost 10.25 % of an hour-long recording.
    ///
    /// `drain(..FRAME_INTERLEAVED)` shifted the whole remaining buffer on every
    /// frame, so consuming a backlog of n frames moved about n squared over two
    /// samples. The further behind the encoder fell the more each frame cost,
    /// which is a feedback loop rather than a constant penalty.
    ///
    /// The bound below is deliberately generous - linear with a factor of two.
    /// The old implementation exceeds it by orders of magnitude, and no
    /// plausible correct implementation comes near it.
    #[test]
    fn compaction_stays_linear_under_backlog() {
        let directory = scratch("backlog");
        let mut writer = SegmentedOpusWriter::new(&directory, "seg", 24_000, 5.0)
            .expect("the writer should open");

        // A backlog, then a small remainder: the shape that made `drain` fold.
        // Each call hands over 200 whole frames plus a few stray samples, so
        // the old code shifted a shrinking tail two hundred times per call.
        const BURSTS: usize = 20;
        const FRAMES_PER_BURST: usize = 200;
        let burst = vec![0.0_f32; FRAME_INTERLEAVED * FRAMES_PER_BURST + 7];

        for _ in 0..BURSTS {
            writer.write(&burst).expect("writing should succeed");
        }

        let fed = (burst.len() * BURSTS) as u64;
        assert!(
            writer.samples_moved() <= fed * 2,
            "compaction moved {} samples for {fed} fed - that is superlinear",
            writer.samples_moved()
        );

        let _ = writer.finish("mic", "sys", 0);
        let _ = std::fs::remove_dir_all(&directory);
    }

    /// The steady state is the one that runs for an hour: chunks arriving a
    /// little faster than one frame, for hundreds of thousands of frames.
    ///
    /// What matters here is memory, not copies. The buffer holds what has not
    /// yet made a whole frame and nothing else, so it stays within a frame
    /// however long the recording runs - which is the difference between the
    /// 668 MB measured over an hour and a flat few kilobytes.
    #[test]
    fn the_buffer_stays_within_a_frame_however_long_it_runs() {
        let directory = scratch("steady");
        let mut writer = SegmentedOpusWriter::new(&directory, "seg", 24_000, 5.0)
            .expect("the writer should open");

        let chunk = vec![0.0_f32; FRAME_INTERLEAVED / 2 + 3];
        for round in 0..2_000 {
            writer.write(&chunk).expect("writing should succeed");
            assert!(
                writer.buffered() < FRAME_INTERLEAVED,
                "round {round}: {} samples buffered, more than one frame",
                writer.buffered()
            );
        }

        // And the copying stayed proportional to what went in, rather than to
        // the square of it. Compaction here moves only the few samples left
        // over from each frame, which is the amortisation working, not a leak.
        let fed = (chunk.len() * 2_000) as u64;
        assert!(
            writer.samples_moved() <= fed,
            "moved {} samples for {fed} fed",
            writer.samples_moved()
        );

        let _ = writer.finish("mic", "sys", 0);
        let _ = std::fs::remove_dir_all(&directory);
    }

    /// The cursor must not swallow the tail: `finish` pads whatever is left of
    /// the live region, not of a buffer still holding everything encoded.
    #[test]
    fn the_tail_after_compaction_is_still_flushed() {
        let directory = scratch("tail");
        let mut writer = SegmentedOpusWriter::new(&directory, "seg", 24_000, 5.0)
            .expect("the writer should open");

        // Enough to force at least one compaction, then a partial frame left
        // over that only `finish` can emit.
        writer
            .write(&vec![0.25_f32; FRAME_INTERLEAVED * 40])
            .expect("writing should succeed");
        writer
            .write(&vec![0.25_f32; FRAME_INTERLEAVED / 3])
            .expect("writing should succeed");

        let before = writer.total_samples();
        let manifest = writer
            .finish("mic", "sys", 0)
            .expect("finish should succeed");

        assert!(
            manifest.duration_ms > 0,
            "the flushed tail should extend the recording"
        );
        assert!(
            manifest.duration_ms * u64::from(TARGET_SAMPLE_RATE) / 1000 > before,
            "the partial frame left after compaction was dropped"
        );

        let _ = std::fs::remove_dir_all(&directory);
    }

    use super::{
        samples_to_ms, SegmentedOpusWriter, FRAME_INTERLEAVED, FRAME_SAMPLES, GRANULE_PER_SAMPLE,
    };
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

    /// One Ogg page, as it sits in the byte stream.
    #[derive(Debug)]
    struct Page {
        beginning: bool,
        end: bool,
        granule: u64,
        serial: u32,
        sequence: u32,
        length: usize,
    }

    /// Walk a byte stream as Ogg pages, checking each page's own checksum.
    ///
    /// The checksum is the point. Everything else a test could assert about
    /// this file - the headers are there, the timestamps rise - would still
    /// pass if a page had been cut in half and glued to another, which is
    /// exactly what segmenting a stream can get wrong. A page whose CRC
    /// matches is a page that arrived whole.
    fn pages(bytes: &[u8]) -> Vec<Page> {
        /// Ogg uses CRC-32 with the polynomial below, no reflection and no
        /// final inversion - not the CRC-32 of zip or PNG.
        fn crc32(data: &[u8]) -> u32 {
            let mut crc = 0_u32;
            for byte in data {
                crc ^= u32::from(*byte) << 24;
                for _ in 0..8 {
                    crc = if crc & 0x8000_0000 == 0 {
                        crc << 1
                    } else {
                        (crc << 1) ^ 0x04c1_1db7
                    };
                }
            }
            crc
        }

        let mut pages = Vec::new();
        let mut at = 0;
        while at < bytes.len() {
            assert_eq!(&bytes[at..at + 4], b"OggS", "no page header at byte {at}");
            assert_eq!(bytes[at + 4], 0, "unknown Ogg version");
            let flags = bytes[at + 5];
            let count = bytes[at + 26] as usize;
            let header = 27 + count;
            let payload: usize = bytes[at + 27..at + header]
                .iter()
                .map(|lacing| *lacing as usize)
                .sum();
            let length = header + payload;

            let mut whole = bytes[at..at + length].to_vec();
            let declared = u32::from_le_bytes([whole[22], whole[23], whole[24], whole[25]]);
            whole[22..26].fill(0);
            assert_eq!(
                crc32(&whole),
                declared,
                "page {} does not checksum: it was cut or reassembled wrongly",
                pages.len()
            );

            pages.push(Page {
                beginning: flags & 0x02 != 0,
                end: flags & 0x04 != 0,
                granule: u64::from_le_bytes(bytes[at + 6..at + 14].try_into().expect("8 bytes")),
                serial: u32::from_le_bytes(bytes[at + 14..at + 18].try_into().expect("4 bytes")),
                sequence: u32::from_le_bytes(bytes[at + 18..at + 22].try_into().expect("4 bytes")),
                length,
            });
            at += length;
        }
        pages
    }

    /// The segments are pieces of one file, not a pile of little files.
    ///
    /// This is what the server is sent: `finalize-local` takes one object and
    /// the transcription provider is handed one URL, so the uploader puts the
    /// segments back together end to end. That only produces a valid recording
    /// if the encoder wrote one logical stream and cut it at page boundaries -
    /// which is the property below, checked page by page and checksum by
    /// checksum on the concatenation itself.
    #[test]
    fn the_segments_reassemble_into_one_ogg_stream() {
        let dir = scratch("reassemble");
        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        writer.write(&tone(17.0)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");
        assert!(manifest.segments.len() >= 3, "expected several segments");

        // Each segment file holds the audio the manifest says it holds. This
        // is what concatenation cannot check - the joined bytes are the same
        // wherever the cuts fall - and it is what EF-17 rests on: a crash
        // loses the segment in progress, so "at most five seconds" is only
        // true if the segments already stored really contain the rest.
        //
        // The muxer only ever emits whole pages, and it emits them when a page
        // fills rather than when a segment ends. Without a flush at the
        // boundary the first segment comes out holding nothing but the two
        // headers while its manifest entry claims five seconds of audio.
        let mut whole = Vec::new();
        let mut so_far = 0_u64;
        for segment in &manifest.segments {
            let bytes = std::fs::read(dir.join(&segment.file)).expect("segment");
            so_far += segment.samples;

            let within = pages(&bytes);
            assert_eq!(
                within.iter().map(|page| page.length).sum::<usize>(),
                bytes.len(),
                "segment {} ends in the middle of a page",
                segment.index
            );
            assert_eq!(
                within
                    .last()
                    .expect("a segment holds at least one page")
                    .granule,
                so_far * u64::from(GRANULE_PER_SAMPLE),
                "segment {} ends at a different point than the manifest says: \
                 its audio was left in a page that had not been flushed",
                segment.index
            );
            whole.extend_from_slice(&bytes);
        }

        let pages = pages(&whole);
        assert!(pages.len() > manifest.segments.len());
        assert_eq!(
            pages.iter().map(|page| page.length).sum::<usize>(),
            whole.len(),
            "the pages do not account for every byte"
        );

        // One stream: one beginning, one end, one serial, and page numbers
        // that run without a gap or a restart from end to end.
        assert!(pages[0].beginning, "the first page is not a stream start");
        assert_eq!(
            pages.iter().filter(|page| page.beginning).count(),
            1,
            "a second stream starts inside the file"
        );
        assert!(
            pages.last().expect("pages").end,
            "the stream never ends: no page carries the end-of-stream flag"
        );
        assert_eq!(pages.iter().filter(|page| page.end).count(), 1);

        let serial = pages[0].serial;
        assert!(pages.iter().all(|page| page.serial == serial));
        for (expected, page) in pages.iter().enumerate() {
            assert_eq!(
                page.sequence, expected as u32,
                "page numbering restarts mid-file, which is what a stream per segment does"
            );
        }

        // And the headers describe the recording once, not once per segment.
        let occurrences = |needle: &[u8]| {
            whole
                .windows(needle.len())
                .filter(|window| *window == needle)
                .count()
        };
        assert_eq!(occurrences(b"OpusHead"), 1);
        assert_eq!(occurrences(b"OpusTags"), 1);

        // Timestamps run through the whole file rather than restarting at each
        // segment, and the last one is the length of the recording.
        let audio: Vec<u64> = pages.iter().skip(2).map(|page| page.granule).collect();
        assert!(
            audio.windows(2).all(|pair| pair[1] > pair[0]),
            "granule positions do not increase across the file: {audio:?}"
        );
        assert_eq!(
            *audio.last().expect("audio pages"),
            manifest.total_samples * u64::from(GRANULE_PER_SAMPLE),
            "the final timestamp is not the length of the recording"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The reassembled file decodes, and decodes to the whole recording.
    ///
    /// Everything else here reads the container. This plays it: the Ogg
    /// demuxer is the crate's own reader rather than our writer, and the
    /// decoder is the libopus a transcription provider would put it through.
    /// A file that satisfies every structural assertion and still cannot be
    /// decoded would be an hour of meeting nobody can hear.
    #[test]
    fn the_reassembled_file_decodes_to_the_whole_recording() {
        let dir = scratch("decode");
        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        writer.write(&tone(17.0)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");

        let mut whole = Vec::new();
        for segment in &manifest.segments {
            whole.extend_from_slice(&std::fs::read(dir.join(&segment.file)).expect("segment"));
        }

        let mut reader = ogg::PacketReader::new(std::io::Cursor::new(&whole));
        let mut decoder = audiopus::coder::Decoder::new(
            audiopus::SampleRate::Hz16000,
            audiopus::Channels::Stereo,
        )
        .expect("the decoder opens");

        // 120 ms of stereo is more than any packet we produce.
        let mut out = vec![0.0_f32; FRAME_SAMPLES * 2 * 6];
        let mut decoded = 0_u64;
        let mut packets = 0_usize;
        let mut loudest = 0.0_f32;

        while let Some(packet) = reader.read_packet().expect("the stream demuxes") {
            packets += 1;
            // The two headers are not Opus packets.
            if packets <= 2 {
                continue;
            }
            let samples = decoder
                .decode_float(Some(&packet.data[..]), &mut out[..], false)
                .unwrap_or_else(|error| panic!("packet {packets} did not decode: {error}"));
            decoded += samples as u64;
            for sample in &out[..samples * 2] {
                loudest = loudest.max(sample.abs());
            }
        }

        assert_eq!(
            decoded, manifest.total_samples,
            "the file decodes to a different length than the manifest claims"
        );
        assert!(
            loudest > 0.2,
            "the file decodes to near-silence ({loudest:.3}); a 440 Hz tone went in"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// RFC 7845: the pre-skip is counted in 48 kHz samples whatever the rate
    /// the stream was encoded at. Writing the encoder's own figure declared a
    /// third of the real delay, and a decoder trimming a third of the encoder
    /// lead-in leaves the rest of it at the front of the recording.
    #[test]
    fn the_pre_skip_is_written_in_forty_eight_kilohertz_samples() {
        let dir = scratch("preskip");
        let mut writer = SegmentedOpusWriter::new(&dir, "test", 32_000, 5.0).expect("writer");
        writer.write(&tone(6.0)).expect("write");
        let manifest = writer.finish("mic", "speakers", 0).expect("finish");

        let first = std::fs::read(dir.join(&manifest.segments[0].file)).expect("segment");
        let head = first
            .windows(8)
            .position(|window| window == b"OpusHead")
            .expect("the stream declares no OpusHead");
        let pre_skip = u16::from_le_bytes([first[head + 10], first[head + 11]]);

        // libopus reports 6.5 ms of lookahead at 16 kHz, which is 104 samples
        // there and 312 at the rate the container counts in.
        assert!(
            pre_skip >= 240,
            "pre-skip of {pre_skip} is too small to be a 48 kHz figure"
        );
        assert!(pre_skip <= 1000, "pre-skip of {pre_skip} is implausible");

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
