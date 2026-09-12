//! The vault on disk: where segments go, and what survives a power cut.
//!
//! Layout, from section 16.3:
//!
//! ```text
//! %LOCALAPPDATA%\NovaBrief\recordings\{meeting_id}\
//!     manifest.json
//!     0000.seg
//!     0001.seg
//!     ...
//! ```
//!
//! The ordering of writes is the whole design, so it is worth stating once:
//!
//! 1. the segment is written and flushed to the platter;
//! 2. *then* the manifest is updated, atomically.
//!
//! Never the other way round. A manifest that names a segment which is not
//! fully on disk turns a crash into corruption; a segment that no manifest
//! names is merely five seconds nobody claimed, which is exactly the loss
//! EF-17 allows.

use std::io::Write;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::error::{Result, VaultError};
use crate::keys::{AccountKey, MeetingKey};
use crate::manifest::{Manifest, RecordingState, SegmentRecord};
use crate::seal::Sealer;

/// The root of one machine's recordings.
#[derive(Debug)]
pub struct Vault<S: Sealer> {
    root: PathBuf,
    account: AccountKey,
    sealer: S,
}

/// One recording being written.
#[derive(Debug)]
pub struct Recording {
    directory: PathBuf,
    manifest: Manifest,
    key: MeetingKey,
}

impl<S: Sealer> Vault<S> {
    /// Open the vault at `root`, under `account`.
    ///
    /// The key is handed in rather than derived here, and that is deliberate.
    /// It used to take a refresh token and derive the key itself, which looked
    /// like what section 16.3 asks for and quietly broke every recording each
    /// time the session refreshed - refresh tokens rotate. Taking the key means
    /// the caller has to say where it came from, and [`crate::DeviceSecret`] is
    /// the only thing that produces a stable one.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>, account: AccountKey, sealer: S) -> Self {
        Self {
            root: root.into(),
            account,
            sealer,
        }
    }

    /// Where one recording lives.
    #[must_use]
    pub fn directory_of(&self, meeting_id: &str) -> PathBuf {
        self.root.join(meeting_id)
    }

    /// Start a recording.
    ///
    /// The manifest is written before any audio arrives, so a machine that dies
    /// one second into a meeting still leaves something recovery can find and
    /// report. A recording that exists only in memory until the first segment
    /// lands is a recording nobody can tell you about afterwards.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the directory or the manifest cannot be written,
    /// [`VaultError::KeyStore`] if the key cannot be generated or sealed.
    pub fn begin(
        &self,
        meeting_id: &str,
        debug_id: &str,
        input_device: &str,
        output_device: &str,
    ) -> Result<Recording> {
        let directory = self.directory_of(meeting_id);
        std::fs::create_dir_all(&directory).map_err(|source| VaultError::Io {
            path: directory.clone(),
            source,
        })?;

        let key = MeetingKey::generate()?;
        let sealed_by_account = self.account.seal(&key)?;
        let sealed_key = self.sealer.seal(&sealed_by_account)?;

        let manifest = Manifest {
            version: Manifest::VERSION,
            meeting_id: meeting_id.to_owned(),
            debug_id: debug_id.to_owned(),
            state: RecordingState::Recording,
            sealed_key,
            segments: Vec::new(),
            input_device: input_device.to_owned(),
            output_device: output_device.to_owned(),
        };
        manifest.save_atomically(&manifest_path(&directory))?;

        Ok(Recording {
            directory,
            manifest,
            key,
        })
    }

    /// Reopen a recording left behind by a crash.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] or [`VaultError::Manifest`] if the manifest cannot be
    /// read, [`VaultError::Decryption`] if this session cannot open its key -
    /// a different Windows user, or a refresh token that has been revoked.
    pub fn reopen(&self, meeting_id: &str) -> Result<Recording> {
        let directory = self.directory_of(meeting_id);
        let manifest = Manifest::load(&manifest_path(&directory))?;
        let sealed_by_account = self.sealer.unseal(&manifest.sealed_key)?;
        let key = self.account.unseal(&sealed_by_account)?;

        Ok(Recording {
            directory,
            manifest,
            key,
        })
    }

    /// Every recording on this machine, whatever state it is in.
    ///
    /// Directories that hold no readable manifest are skipped rather than
    /// reported: a half-created directory is not a meeting, and startup is not
    /// the moment to stop on somebody else's litter.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the root cannot be listed. A missing root is not
    /// an error - it is a machine that has never recorded.
    pub fn list(&self) -> Result<Vec<Manifest>> {
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(source) => {
                return Err(VaultError::Io {
                    path: self.root.clone(),
                    source,
                })
            }
        };

        let mut found: Vec<Manifest> = entries
            .filter_map(std::result::Result::ok)
            .filter(|entry| entry.path().is_dir())
            .filter_map(|entry| Manifest::load(&manifest_path(&entry.path())).ok())
            .collect();
        found.sort_by(|a, b| a.meeting_id.cmp(&b.meeting_id));
        Ok(found)
    }

    /// Recordings a crash left mid-flight, marked as interrupted.
    ///
    /// Section 16.3: on start-up the application looks for meetings in
    /// RECORDING or UPLOADING and resumes. Marking them here, once, means the
    /// rest of the application never has to distinguish "still going" from
    /// "was going when the lights went out" - by the time it looks, only one of
    /// those is possible.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the root cannot be listed or a manifest cannot be
    /// rewritten.
    pub fn recover_interrupted(&self) -> Result<Vec<Manifest>> {
        let mut recovered = Vec::new();
        for mut manifest in self.list()? {
            if manifest.state != RecordingState::Recording {
                continue;
            }
            manifest.state = RecordingState::Interrupted;
            manifest.save_atomically(&manifest_path(&self.directory_of(&manifest.meeting_id)))?;
            recovered.push(manifest);
        }
        Ok(recovered)
    }

    /// Delete a recording and everything in it.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the directory cannot be removed. A directory that
    /// is already gone is a success: the caller wanted it absent.
    pub fn purge(&self, meeting_id: &str) -> Result<()> {
        let directory = self.directory_of(meeting_id);
        match std::fs::remove_dir_all(&directory) {
            Ok(()) => Ok(()),
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(VaultError::Io {
                path: directory,
                source,
            }),
        }
    }
}

impl Recording {
    /// What is known about this recording.
    #[must_use]
    pub const fn manifest(&self) -> &Manifest {
        &self.manifest
    }

    /// Encrypt one segment, put it on the disk, and record it.
    ///
    /// Returns the index it was given.
    ///
    /// # Errors
    ///
    /// [`VaultError::SegmentOutOfOrder`] if `index` is not the next one -
    /// refused rather than tolerated, because the index is the AES-GCM nonce
    /// and a repeat under one key leaks the authentication key. [`VaultError::Io`]
    /// if the write or the manifest update fails.
    pub fn append(&mut self, index: u32, audio: &[u8], duration_ms: u64) -> Result<u32> {
        let expected = self.manifest.next_index();
        if index != expected {
            return Err(VaultError::SegmentOutOfOrder {
                got: index,
                expected,
            });
        }

        let sealed = self
            .key
            .encrypt_segment(&self.manifest.meeting_id, index, audio)?;
        let file = format!("{index:04}.seg");
        let path = self.directory.join(&file);

        // Flushed before the manifest names it. The other order turns a power
        // cut into a manifest pointing at bytes that never arrived.
        let write = || -> std::io::Result<()> {
            let mut handle = std::fs::File::create(&path)?;
            handle.write_all(&sealed)?;
            handle.sync_all()
        };
        write().map_err(|source| VaultError::Io {
            path: path.clone(),
            source,
        })?;

        self.manifest.segments.push(SegmentRecord {
            index,
            file,
            bytes: sealed.len() as u64,
            // Of the ciphertext: it is what is on the disk, so a later
            // integrity check needs no key.
            sha256: hex(&Sha256::digest(&sealed)),
            duration_ms,
        });
        self.manifest
            .save_atomically(&manifest_path(&self.directory))?;
        Ok(index)
    }

    /// Read one segment back.
    ///
    /// # Errors
    ///
    /// [`VaultError::Manifest`] if no such segment is recorded,
    /// [`VaultError::Io`] if it cannot be read, [`VaultError::Decryption`] if
    /// its contents do not match what was written.
    pub fn read(&self, index: u32) -> Result<Vec<u8>> {
        let record = self
            .manifest
            .segments
            .iter()
            .find(|segment| segment.index == index)
            .ok_or_else(|| VaultError::Manifest(format!("no segment {index}")))?;

        let path = self.directory.join(&record.file);
        let sealed = std::fs::read(&path).map_err(|source| VaultError::Io { path, source })?;
        self.key
            .decrypt_segment(&self.manifest.meeting_id, index, &sealed)
    }

    /// Move the recording to a new state.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the manifest cannot be rewritten.
    pub fn set_state(&mut self, state: RecordingState) -> Result<()> {
        self.manifest.state = state;
        self.manifest
            .save_atomically(&manifest_path(&self.directory))
    }

    /// Segments on disk that the manifest does not name.
    ///
    /// The five seconds EF-17 allows losing. A segment is flushed before the
    /// manifest names it, so a crash in that window leaves exactly this: a file
    /// that is complete and unclaimed. Reporting it rather than deleting it
    /// keeps the choice with the caller - for a meeting that is about to be
    /// uploaded anyway, five recoverable seconds are worth having.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the directory cannot be listed.
    pub fn orphan_segments(&self) -> Result<Vec<PathBuf>> {
        let known: std::collections::HashSet<&str> = self
            .manifest
            .segments
            .iter()
            .map(|segment| segment.file.as_str())
            .collect();

        let entries = std::fs::read_dir(&self.directory).map_err(|source| VaultError::Io {
            path: self.directory.clone(),
            source,
        })?;

        let mut orphans: Vec<PathBuf> = entries
            .filter_map(std::result::Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().is_some_and(|extension| extension == "seg"))
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_none_or(|name| !known.contains(name))
            })
            .collect();
        orphans.sort();
        Ok(orphans)
    }
}

fn manifest_path(directory: &Path) -> PathBuf {
    directory.join("manifest.json")
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut out, byte| {
        use std::fmt::Write as _;
        let _ = write!(out, "{byte:02x}");
        out
    })
}

#[cfg(test)]
mod tests {
    use super::{manifest_path, Vault};
    use crate::keys::AccountKey;
    use crate::manifest::RecordingState;
    use crate::seal::PassthroughSealer;

    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-store-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    fn vault(root: &std::path::Path) -> Vault<PassthroughSealer> {
        Vault::new(
            root,
            AccountKey::derive(b"a-device-secret"),
            PassthroughSealer,
        )
    }

    #[test]
    fn a_segment_survives_a_round_trip() {
        let root = scratch("roundtrip");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");

        let audio = vec![42_u8; 8192];
        recording.append(0, &audio, 5000).expect("appends");

        assert_eq!(recording.read(0).expect("reads"), audio);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The bytes on disk must not be the audio. This is the whole of EF-17's
    /// answer to a stolen laptop.
    #[test]
    fn what_lands_on_the_disk_is_not_the_audio() {
        let root = scratch("ciphertext");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");

        let audio = b"the quarterly numbers are".repeat(64);
        recording.append(0, &audio, 5000).expect("appends");

        let on_disk = std::fs::read(root.join("mtg-1").join("0000.seg")).expect("reads the file");
        assert_ne!(on_disk, audio);
        assert!(
            !on_disk
                .windows(b"quarterly".len())
                .any(|window| window == b"quarterly"),
            "plaintext survived into the file"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The index is the nonce. A repeat under one key leaks GCM's
    /// authentication key, so it is refused rather than tolerated.
    #[test]
    fn a_repeated_segment_index_is_refused() {
        let root = scratch("repeat");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");

        recording.append(0, b"first", 5000).expect("appends");
        assert!(recording.append(0, b"second", 5000).is_err());
        assert!(recording.append(2, b"skipped", 5000).is_err(), "gaps too");
        assert!(recording.append(1, b"next", 5000).is_ok());
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Section 16.3: at start-up, a meeting left in RECORDING is a crash.
    #[test]
    fn a_recording_left_open_is_found_and_marked_interrupted() {
        let root = scratch("recover");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        recording.append(0, b"audio", 5000).expect("appends");
        drop(recording); // the process dies here

        let recovered = store.recover_interrupted().expect("recovers");
        assert_eq!(recovered.len(), 1);
        assert_eq!(recovered[0].meeting_id, "mtg-1");
        assert_eq!(recovered[0].state, RecordingState::Interrupted);

        // And reopening gives the audio back, which is the point of recovering.
        let reopened = store.reopen("mtg-1").expect("reopens");
        assert_eq!(reopened.read(0).expect("reads"), b"audio");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// EF-17, measured the way the brief measures it. A crash between the
    /// segment reaching the disk and the manifest naming it must cost that
    /// segment and nothing more.
    #[test]
    fn a_crash_between_the_segment_and_the_manifest_costs_only_that_segment() {
        let root = scratch("crash");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        recording
            .append(0, b"first five seconds", 5000)
            .expect("appends");
        recording
            .append(1, b"second five seconds", 5000)
            .expect("appends");

        // The crash: a segment fully written, the manifest not yet updated.
        std::fs::write(root.join("mtg-1").join("0002.seg"), b"orphaned ciphertext")
            .expect("writes the orphan");

        let reopened = store.reopen("mtg-1").expect("reopens");
        assert_eq!(
            reopened.manifest().segments.len(),
            2,
            "the manifest is intact"
        );
        assert_eq!(reopened.read(0).expect("reads"), b"first five seconds");
        assert_eq!(reopened.read(1).expect("reads"), b"second five seconds");
        assert_eq!(reopened.orphan_segments().expect("lists").len(), 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A revoked session cannot open what it recorded. EF-03 revokes the
    /// refresh token; the account key derives from it, so the recordings go
    /// with it.
    #[test]
    fn another_session_cannot_reopen_a_recording() {
        let root = scratch("session");
        let mine = vault(&root);
        let mut recording = mine
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        recording.append(0, b"audio", 5000).expect("appends");

        let theirs = Vault::new(
            &root,
            AccountKey::derive(b"another-device"),
            PassthroughSealer,
        );
        assert!(theirs.reopen("mtg-1").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn listing_a_machine_that_never_recorded_is_empty_not_an_error() {
        let store = vault(&scratch("never"));
        assert_eq!(store.list().expect("lists").len(), 0);
    }

    /// Startup must not stop on somebody else's litter.
    #[test]
    fn a_directory_without_a_manifest_is_skipped() {
        let root = scratch("litter");
        let store = vault(&root);
        store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        std::fs::create_dir_all(root.join("not-a-meeting")).expect("creates");

        assert_eq!(store.list().expect("lists").len(), 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn purging_removes_everything_and_is_idempotent() {
        let root = scratch("purge");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        recording.append(0, b"audio", 5000).expect("appends");

        store.purge("mtg-1").expect("purges");
        assert!(!manifest_path(&store.directory_of("mtg-1")).exists());
        store.purge("mtg-1").expect("purging twice is fine");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A meeting exists on disk before any audio arrives, so a machine that
    /// dies one second in still leaves something to report.
    #[test]
    fn a_recording_exists_before_its_first_segment() {
        let root = scratch("empty");
        let store = vault(&root);
        store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");

        let listed = store.list().expect("lists");
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].segments.len(), 0);
        assert_eq!(listed[0].duration_ms(), 0);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn an_uploaded_recording_is_not_treated_as_a_crash() {
        let root = scratch("uploaded");
        let store = vault(&root);
        let mut recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("begins");
        recording.set_state(RecordingState::Uploaded).expect("sets");

        assert_eq!(store.recover_interrupted().expect("recovers").len(), 0);
        let _ = std::fs::remove_dir_all(&root);
    }
}
