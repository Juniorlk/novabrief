//! The manifest, and the reason it is written the way it is.
//!
//! EF-17 is measured by pulling the plug: "a brutal stop loses at most the last
//! five seconds". Almost all of that promise lives here rather than in the
//! encryption, because the manifest is the single file whose loss costs the
//! *whole* recording rather than one segment of it.
//!
//! So it is never written in place. A rewrite that is interrupted half way
//! leaves a truncated JSON document, and a truncated manifest is a meeting
//! nobody can reassemble - the exact failure the criterion forbids, converted
//! from five seconds into an hour.

use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{Result, VaultError};

/// Where a recording is in its life.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordingState {
    /// Segments are still arriving.
    Recording,
    /// Capture finished; the segments are on their way to the server.
    Uploading,
    /// The server has the recording. Local files wait out their retention.
    Uploaded,
    /// Capture stopped without being finalised - a crash, or the power going.
    /// Set by recovery, never by the recorder.
    Interrupted,
}

/// One five-second segment on disk.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentRecord {
    /// Position in the recording. Also the AES-GCM nonce, so it never repeats.
    pub index: u32,
    /// File name inside the meeting directory.
    pub file: String,
    /// Bytes on disk, after encryption.
    pub bytes: u64,
    /// SHA-256 of the **ciphertext**, which is what is actually on the disk
    /// and therefore what a later read can check without a key.
    pub sha256: String,
    /// Milliseconds of audio this segment carries.
    pub duration_ms: u64,
}

/// What the vault knows about one recording.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    /// Format version, so a future change can be recognised rather than
    /// guessed at.
    pub version: u32,
    /// Server-side meeting identifier. Bound into every segment as associated
    /// data.
    pub meeting_id: String,
    /// ADR-07's identifier, carried from the API to the desktop logs.
    pub debug_id: String,
    /// Where this recording is.
    pub state: RecordingState,
    /// The meeting key, sealed by the account key and then by DPAPI.
    #[serde(with = "hex_bytes")]
    pub sealed_key: Vec<u8>,
    /// Segments written and durable, in order.
    pub segments: Vec<SegmentRecord>,
    /// Devices the audio came from, for the server-side manifest.
    pub input_device: String,
    /// Render device the system audio was captured from.
    pub output_device: String,
}

impl Manifest {
    /// The current format version.
    pub const VERSION: u32 = 1;

    /// Total audio recorded so far, in milliseconds.
    #[must_use]
    pub fn duration_ms(&self) -> u64 {
        self.segments
            .iter()
            .map(|segment| segment.duration_ms)
            .sum()
    }

    /// The index the next segment must carry.
    #[must_use]
    pub fn next_index(&self) -> u32 {
        self.segments.last().map_or(0, |segment| segment.index + 1)
    }

    /// Read a manifest from disk.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if it cannot be read, [`VaultError::Manifest`] if the
    /// contents are not a manifest this version understands.
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|source| VaultError::Io {
            path: path.to_path_buf(),
            source,
        })?;
        let manifest: Self = serde_json::from_slice(&bytes)
            .map_err(|error| VaultError::Manifest(error.to_string()))?;
        if manifest.version != Self::VERSION {
            return Err(VaultError::Manifest(format!(
                "version {} was written by another release",
                manifest.version
            )));
        }
        Ok(manifest)
    }

    /// Write the manifest so that a power cut cannot leave it half written.
    ///
    /// Into a temporary file, flushed to the platter, then renamed over the
    /// real one. Rename is the only filesystem operation that is atomic across
    /// a crash: afterwards the name points at the whole new file or at the
    /// whole old one, never at a prefix of either.
    ///
    /// `sync_all` before the rename is the half people forget. Without it the
    /// rename can reach the disk before the bytes it points at, and the
    /// recording ends up with a manifest naming segments whose contents never
    /// arrived - which looks like corruption rather than like a crash.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if any step fails. The temporary file is removed on
    /// the way out so a failed write does not leave litter behind.
    pub fn save_atomically(&self, path: &Path) -> Result<()> {
        let json = serde_json::to_vec_pretty(self)
            .map_err(|error| VaultError::Manifest(error.to_string()))?;

        let temporary = temporary_path(path);
        let write = || -> std::io::Result<()> {
            let mut file = File::create(&temporary)?;
            file.write_all(&json)?;
            file.sync_all()
        };
        if let Err(source) = write() {
            let _ = std::fs::remove_file(&temporary);
            return Err(VaultError::Io {
                path: temporary,
                source,
            });
        }

        std::fs::rename(&temporary, path).map_err(|source| {
            let _ = std::fs::remove_file(&temporary);
            VaultError::Io {
                path: path.to_path_buf(),
                source,
            }
        })
    }
}

/// The scratch name a manifest is written under before being renamed into
/// place. Next to the target, because rename is only atomic within a volume.
fn temporary_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".writing");
    path.with_file_name(name)
}

/// Hex, because JSON has no bytes and base64 would need another dependency for
/// a field written once per recording.
mod hex_bytes {
    use serde::{Deserialize, Deserializer, Serializer};

    pub(super) fn serialize<S: Serializer>(bytes: &[u8], serializer: S) -> Result<S::Ok, S::Error> {
        let mut out = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            out.push_str(&format!("{byte:02x}"));
        }
        serializer.serialize_str(&out)
    }

    pub(super) fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<u8>, D::Error> {
        let text = String::deserialize(deserializer)?;
        if text.len() % 2 != 0 {
            return Err(serde::de::Error::custom("odd number of hex digits"));
        }
        (0..text.len())
            .step_by(2)
            .map(|index| {
                u8::from_str_radix(&text[index..index + 2], 16).map_err(serde::de::Error::custom)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::{temporary_path, Manifest, RecordingState, SegmentRecord};
    use std::path::Path;

    fn manifest() -> Manifest {
        Manifest {
            version: Manifest::VERSION,
            meeting_id: "mtg-1".to_owned(),
            debug_id: "DBG-MTG-20260912-0001".to_owned(),
            state: RecordingState::Recording,
            sealed_key: vec![1, 2, 3, 4],
            segments: vec![SegmentRecord {
                index: 0,
                file: "0000.seg".to_owned(),
                bytes: 1024,
                sha256: "ab".repeat(32),
                duration_ms: 5000,
            }],
            input_device: "mic".to_owned(),
            output_device: "speakers".to_owned(),
        }
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-vault-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("the scratch directory is creatable");
        dir
    }

    #[test]
    fn a_manifest_round_trips() {
        let dir = scratch("roundtrip");
        let path = dir.join("manifest.json");
        let original = manifest();

        original.save_atomically(&path).expect("saves");
        let loaded = Manifest::load(&path).expect("loads");

        assert_eq!(loaded, original);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The whole point of the temporary file: a rewrite that never completes
    /// must leave the previous manifest intact and readable.
    ///
    /// The interruption is simulated by writing the scratch file and stopping,
    /// which is the state a power cut leaves behind - the rename is the step
    /// that never ran.
    #[test]
    fn an_interrupted_rewrite_leaves_the_previous_manifest_readable() {
        let dir = scratch("interrupted");
        let path = dir.join("manifest.json");
        let first = manifest();
        first.save_atomically(&path).expect("saves");

        // A crash during the next save: the scratch file exists, half written,
        // and the rename never happened.
        std::fs::write(temporary_path(&path), b"{\"version\": 1, \"meeting_i")
            .expect("the scratch file is writable");

        let loaded = Manifest::load(&path).expect("the previous manifest survives");
        assert_eq!(
            loaded, first,
            "a crash cost more than the segment in flight"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_next_index_follows_the_last_segment() {
        let mut m = manifest();
        assert_eq!(m.next_index(), 1);
        m.segments.clear();
        assert_eq!(m.next_index(), 0, "an empty recording starts at zero");
    }

    #[test]
    fn the_duration_is_the_sum_of_the_segments() {
        let mut m = manifest();
        m.segments.push(SegmentRecord {
            index: 1,
            file: "0001.seg".to_owned(),
            bytes: 512,
            sha256: "cd".repeat(32),
            duration_ms: 5000,
        });
        assert_eq!(m.duration_ms(), 10_000);
    }

    /// A manifest from a future release is refused rather than misread. Half
    /// understanding a recording is worse than declining it.
    #[test]
    fn a_manifest_from_another_version_is_refused() {
        let dir = scratch("version");
        let path = dir.join("manifest.json");
        let mut m = manifest();
        m.version = 99;
        m.save_atomically(&path).expect("saves");

        assert!(Manifest::load(&path).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_truncated_manifest_is_an_error_not_a_panic() {
        let dir = scratch("truncated");
        let path = dir.join("manifest.json");
        std::fs::write(&path, b"{\"version\": 1, \"meeting_i").expect("writes");

        assert!(Manifest::load(&path).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_scratch_file_sits_beside_its_target() {
        // Rename is only atomic within a volume, so the temporary file cannot
        // live in the system temp directory.
        let path = Path::new("C:/data/meeting/manifest.json");
        assert_eq!(temporary_path(path).parent(), path.parent());
    }
}
