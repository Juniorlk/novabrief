//! What survives a restart in the middle of an upload.
//!
//! EF-18 asks the upload to resume at the last confirmed part. Within one run
//! that is a variable; across a crash, a reboot, or a laptop closed on the way
//! home, it has to be on the disk - and it has to be on the disk *before* the
//! next part is sent, or a crash in between would lose the only record that
//! the previous one was accepted.
//!
//! Nothing here is audio, so nothing here is encrypted: identifiers, a digest
//! and the tags the store handed back. It sits beside the recording so that
//! deleting the recording deletes it too.

use std::path::{Path, PathBuf};

use api_client::UploadedPart;
use serde::{Deserialize, Serialize};

use crate::UploadError;

/// How far an upload has got.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Progress {
    /// The meeting the server knows, which the recording is being sent to.
    ///
    /// Kept here because a laptop may record with no network at all (ADR-05):
    /// the meeting is declared when one appears, and this is what stops a
    /// second declaration from creating a second meeting for the same audio.
    pub meeting_id: String,
    /// ADR-07.
    pub debug_id: String,
    /// The multipart upload at the store.
    pub upload_id: String,
    /// What was declared, so a resumed upload declares the same thing.
    pub size_bytes: u64,
    /// The same.
    pub sha256: String,
    /// Parts the store has accepted, and the tags it recognises them by.
    pub confirmed: Vec<UploadedPart>,
}

impl Progress {
    /// Where the record of an upload lives, beside its recording.
    #[must_use]
    pub fn path_in(directory: &Path) -> PathBuf {
        directory.join("upload.json")
    }

    /// Read it back, or `None` if this upload has not started.
    ///
    /// A file that cannot be parsed is treated as absent rather than as an
    /// error: it means a half-written record from an older version or a bad
    /// shutdown, and the answer is to upload again, not to strand the meeting.
    #[must_use]
    pub fn load(path: &Path) -> Option<Self> {
        let text = std::fs::read_to_string(path).ok()?;
        serde_json::from_str(&text).ok()
    }

    /// Write it, atomically.
    ///
    /// Temporary file, flushed, then renamed. A record rewritten in place and
    /// interrupted would be neither the old state nor the new one, and the
    /// upload would resume from a part list that never existed.
    ///
    /// # Errors
    ///
    /// [`UploadError::Io`] if it cannot be written.
    pub fn save(&self, path: &Path) -> Result<(), UploadError> {
        let json = serde_json::to_vec_pretty(self).map_err(|error| UploadError::Io {
            path: path.to_path_buf(),
            source: std::io::Error::other(error.to_string()),
        })?;

        let temporary = path.with_extension("json.tmp");
        let write = || -> std::io::Result<()> {
            use std::io::Write as _;
            let mut file = std::fs::File::create(&temporary)?;
            file.write_all(&json)?;
            file.sync_all()?;
            std::fs::rename(&temporary, path)
        };
        write().map_err(|source| UploadError::Io {
            path: path.to_path_buf(),
            source,
        })
    }

    /// Forget it, once the meeting is the server's problem.
    ///
    /// # Errors
    ///
    /// Never. A record that cannot be removed is harmless - the next upload of
    /// the same recording would find a finished meeting and stop.
    pub fn forget(path: &Path) {
        let _ = std::fs::remove_file(path);
    }

    /// Whether this part has already been accepted.
    #[must_use]
    pub fn holds(&self, part_number: u32) -> bool {
        self.confirmed
            .iter()
            .any(|part| part.part_number == part_number)
    }

    /// Note that the store accepted a part.
    pub fn accept(&mut self, part: UploadedPart) {
        if !self.holds(part.part_number) {
            self.confirmed.push(part);
        }
    }
}
