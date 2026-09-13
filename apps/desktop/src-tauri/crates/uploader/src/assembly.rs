//! Turning a vault recording back into the file the server expects.
//!
//! The recording is stored as encrypted five-second segments; the server takes
//! one object. The segments are pieces of one Ogg stream, so putting them back
//! end to end *is* the recording - see `audio_engine::encode`.
//!
//! Nothing is ever assembled whole. A four-hour meeting is about 56 MB, and
//! writing it out to be uploaded would put plaintext audio on the disk, which
//! is the one thing EF-17 forbids. So both passes stream: one to measure, one
//! to hand over parts, each holding a segment and at most one part at a time.

use sha2::{Digest, Sha256};
use vault::Recording;

use crate::UploadError;

/// What the recording weighs, and what it hashes to.
///
/// Both are declared to the server before a byte moves, which is what makes
/// the finalisation checkable: the store is asked afterwards what it actually
/// holds, and a truncated upload is refused rather than transcribed into a
/// confident, incomplete report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Measurement {
    /// Size of the reassembled recording.
    pub size_bytes: u64,
    /// SHA-256 of the same bytes, lower-case hexadecimal.
    pub sha256: String,
    /// Audio kept, in milliseconds.
    pub duration_ms: u64,
}

/// Weigh and hash the recording without assembling it.
///
/// # Errors
///
/// [`UploadError::Vault`] if a segment cannot be decrypted, which means this
/// machine or this Windows account is not the one that recorded it.
pub fn measure(recording: &Recording) -> Result<Measurement, UploadError> {
    let manifest = recording.manifest();
    let mut digest = Sha256::new();
    let mut size = 0_u64;

    for segment in &manifest.segments {
        let plain = recording.read(segment.index)?;
        size += plain.len() as u64;
        digest.update(&plain);
    }

    Ok(Measurement {
        size_bytes: size,
        sha256: format!("{:x}", digest.finalize()),
        duration_ms: manifest.duration_ms(),
    })
}

/// The recording, handed over one part at a time.
///
/// Stateful rather than indexed. A `part(n)` that could be called in any order
/// would have to decrypt from the beginning every time, because a segment's
/// plaintext length is not written down anywhere - only its ciphertext length
/// is, and inferring one from the other means writing the cipher's tag size
/// into this file, where an off-by-sixteen would corrupt the upload silently.
#[derive(Debug)]
pub struct Parts<'a> {
    recording: &'a Recording,
    next_segment: usize,
    /// Plaintext read but not yet handed out.
    carry: Vec<u8>,
    part_size: usize,
}

impl<'a> Parts<'a> {
    /// Read `recording` in pieces of `part_size` bytes.
    #[must_use]
    pub fn new(recording: &'a Recording, part_size: u64) -> Self {
        let part_size = usize::try_from(part_size).unwrap_or(usize::MAX);
        Self {
            recording,
            next_segment: 0,
            carry: Vec::with_capacity(part_size),
            part_size: part_size.max(1),
        }
    }

    /// The next part, or `None` once the recording is exhausted.
    ///
    /// Every part is exactly `part_size` bytes except the last, which is
    /// whatever is left - the rule S3 imposes, and the reason the final part
    /// is not padded.
    ///
    /// # Errors
    ///
    /// [`UploadError::Vault`] if a segment cannot be decrypted.
    pub fn next_part(&mut self) -> Result<Option<Vec<u8>>, UploadError> {
        let segments = &self.recording.manifest().segments;
        while self.carry.len() < self.part_size && self.next_segment < segments.len() {
            let index = segments[self.next_segment].index;
            self.carry.extend_from_slice(&self.recording.read(index)?);
            self.next_segment += 1;
        }

        if self.carry.is_empty() {
            return Ok(None);
        }
        let taken = self.part_size.min(self.carry.len());
        let part: Vec<u8> = self.carry.drain(..taken).collect();
        Ok(Some(part))
    }
}
