//! Where the encoder meets the vault.
//!
//! The one place in the product that knows both halves: `audio-engine` turns
//! audio into Ogg/Opus segments and has no opinion about where they go, and
//! `vault` encrypts and stores segments without knowing what is in them. The
//! desktop binary is the composition root, so the join lives here rather than
//! making either crate depend on the other.
//!
//! What the join is *for* is EF-17: plaintext audio must never reach the disk.
//! Since the encoder was changed to hand finished segments to a sink rather
//! than write files itself, the only thing standing between it and the disk is
//! this type - so the test that matters is the one that searches the bytes on
//! disk for the audio and fails if it finds it.

use audio_engine::encode::{EncodeError, Manifest, SegmentSink};
use vault::{Recording, VaultError};

/// Puts every finished segment into the encrypted vault.
#[derive(Debug)]
pub struct VaultSegmentSink {
    recording: Recording,
}

impl VaultSegmentSink {
    /// Wrap a recording the vault has already opened.
    #[must_use]
    pub const fn new(recording: Recording) -> Self {
        Self { recording }
    }

    /// The recording underneath, once the encoder has finished with it.
    #[must_use]
    pub fn into_recording(self) -> Recording {
        self.recording
    }

    /// What the vault knows about this recording.
    #[must_use]
    pub const fn recording(&self) -> &Recording {
        &self.recording
    }
}

impl SegmentSink for VaultSegmentSink {
    fn accept(
        &mut self,
        index: u32,
        bytes: &[u8],
        duration_ms: u64,
    ) -> Result<String, EncodeError> {
        self.recording
            .append(index, bytes, duration_ms)
            .map_err(to_encode_error)?;
        Ok(format!("{index:04}.seg"))
    }

    /// The vault keeps its own manifest, with the sealed key and the recording
    /// state in it. A second one beside it would be a second thing to keep in
    /// agreement, so the encoder's is used in memory and never written.
    fn accept_manifest(&mut self, _manifest: &Manifest) -> Result<(), EncodeError> {
        Ok(())
    }
}

/// Carry a vault failure into the encoder's error type.
///
/// The vault's own message is kept: "this recording cannot be decrypted" and
/// "segment 3 was offered after segment 5" are the useful part, and flattening
/// them into a generic storage error would throw away the only thing that says
/// what to do next.
fn to_encode_error(error: VaultError) -> EncodeError {
    match error {
        VaultError::Io { path, source } => EncodeError::Io { path, source },
        other => EncodeError::Io {
            path: std::path::PathBuf::from("vault"),
            source: std::io::Error::other(other.to_string()),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::VaultSegmentSink;
    use audio_engine::encode::SegmentedOpusWriter;
    use audio_engine::resample::TARGET_SAMPLE_RATE;
    use vault::{DpapiSealer, Vault};

    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-rec-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    /// A recognisable signal: a loud 440 Hz tone, interleaved stereo.
    fn tone(seconds: f64) -> Vec<f32> {
        let frames = (seconds * f64::from(TARGET_SAMPLE_RATE)) as usize;
        (0..frames)
            .flat_map(|n| {
                let value = (n as f32 * 2.0 * std::f32::consts::PI * 440.0 / 16_000.0).sin() * 0.9;
                [value, value * 0.5]
            })
            .collect()
    }

    #[test]
    fn segments_reach_the_vault_and_come_back() {
        let root = scratch("roundtrip");
        let store = Vault::new(&root, b"a-refresh-token", DpapiSealer);
        let recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("the vault opens");

        let mut writer =
            SegmentedOpusWriter::with_sink(VaultSegmentSink::new(recording), 32_000, 5.0)
                .expect("the encoder opens");
        // Twelve seconds: two whole five-second segments and a tail.
        writer.write(&tone(12.0)).expect("encodes");
        let manifest = writer.finish("mic", "speakers", 0).expect("finishes");

        assert!(manifest.segments.len() >= 2, "expected several segments");

        let reopened = store.reopen("mtg-1").expect("reopens");
        assert_eq!(
            reopened.manifest().segments.len(),
            manifest.segments.len(),
            "the vault and the encoder disagree about how many segments there are"
        );
        // And the first one decrypts into something Ogg-shaped.
        let first = reopened.read(0).expect("decrypts");
        assert_eq!(&first[..4], b"OggS", "that is not an Ogg stream");

        let _ = std::fs::remove_dir_all(&root);
    }

    /// EF-17, as a test rather than a claim.
    ///
    /// A 440 Hz tone at 0.9 amplitude is about as recognisable as audio gets.
    /// If any of it survived to the disk in the clear, an Opus stream would be
    /// sitting there with its `OggS` page markers in plain sight.
    #[test]
    fn no_plaintext_audio_reaches_the_disk() {
        let root = scratch("plaintext");
        let store = Vault::new(&root, b"a-refresh-token", DpapiSealer);
        let recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("the vault opens");

        let mut writer =
            SegmentedOpusWriter::with_sink(VaultSegmentSink::new(recording), 32_000, 5.0)
                .expect("the encoder opens");
        writer.write(&tone(12.0)).expect("encodes");
        writer.finish("mic", "speakers", 0).expect("finishes");

        let mut checked = 0_usize;
        for entry in std::fs::read_dir(root.join("mtg-1")).expect("lists") {
            let path = entry.expect("entry").path();
            if path.extension().is_none_or(|extension| extension != "seg") {
                continue;
            }
            let bytes = std::fs::read(&path).expect("reads");
            assert!(
                !bytes.windows(4).any(|window| window == b"OggS"),
                "{}: an Ogg page marker survived to the disk in the clear",
                path.display()
            );
            checked += 1;
        }
        assert!(checked >= 2, "expected to have checked several segments");

        let _ = std::fs::remove_dir_all(&root);
    }

    /// The encoder's manifest is not written next to the segments: the vault
    /// keeps the authoritative one, with the sealed key in it.
    #[test]
    fn only_the_vaults_manifest_is_on_disk() {
        let root = scratch("manifest");
        let store = Vault::new(&root, b"a-refresh-token", DpapiSealer);
        let recording = store
            .begin("mtg-1", "DBG-1", "mic", "speakers")
            .expect("the vault opens");

        let mut writer =
            SegmentedOpusWriter::with_sink(VaultSegmentSink::new(recording), 32_000, 5.0)
                .expect("the encoder opens");
        writer.write(&tone(6.0)).expect("encodes");
        writer.finish("mic", "speakers", 0).expect("finishes");

        let names: Vec<String> = std::fs::read_dir(root.join("mtg-1"))
            .expect("lists")
            .filter_map(std::result::Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();

        assert!(names.contains(&"manifest.json".to_owned()));
        assert!(
            !names.iter().any(|name| name.ends_with("-manifest.json")),
            "the encoder wrote a second manifest: {names:?}"
        );

        let _ = std::fs::remove_dir_all(&root);
    }
}
