//! The refresh token on disk, and the three rules it is stored by.
//!
//! **The password is never stored.** It is sent once and forgotten; what comes
//! back is what persists. EF-11's promise - "once linked, never sign in again
//! unless revoked" - is a property of the refresh token, not a reason to keep
//! the thing that produced it.
//!
//! **The access token never reaches the disk at all.** It lives for minutes,
//! it is a bearer credential for everything the account can do, and writing it
//! down buys nothing: if the process restarts, refreshing costs one request.
//!
//! **The refresh token is written atomically, and that is not a nicety.** It is
//! single-use: presenting it returns a new one, and presenting it twice revokes
//! the whole family (`packages/schemas/auth.py`). So the moment the server
//! answers a refresh, the copy on disk is already dead. A torn write leaves the
//! machine with neither the old token nor the new one, and the person is signed
//! out of a laptop holding meetings that have not been uploaded yet.
//!
//! That window - between the server consuming the old token and this file
//! holding the new one - cannot be closed from here; it is inherent to
//! rotation. It can only be made as short as possible and impossible to leave
//! half done, which is what `rename` gives.

use std::path::PathBuf;

use crate::device::write_atomically;
use crate::error::{Result, VaultError};
use crate::seal::Sealer;

/// Where this installation keeps its session.
#[derive(Debug)]
pub struct CredentialStore<S: Sealer> {
    path: PathBuf,
    sealer: S,
}

impl<S: Sealer> CredentialStore<S> {
    /// A store backed by `path`.
    #[must_use]
    pub fn new(path: impl Into<PathBuf>, sealer: S) -> Self {
        Self {
            path: path.into(),
            sealer,
        }
    }

    /// The refresh token, if this machine has a session.
    ///
    /// A file that cannot be unsealed answers `None` rather than an error: it
    /// means a different Windows user, or a machine whose DPAPI keys have
    /// changed, and in both cases the honest outcome is "you are not signed in
    /// here" rather than an error dialog nobody can act on.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the file exists and cannot be read.
    pub fn refresh_token(&self) -> Result<Option<String>> {
        let sealed = match std::fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(source) => {
                return Err(VaultError::Io {
                    path: self.path.clone(),
                    source,
                })
            }
        };

        let Ok(plain) = self.sealer.unseal(&sealed) else {
            return Ok(None);
        };
        Ok(String::from_utf8(plain).ok())
    }

    /// Replace the stored token.
    ///
    /// # Errors
    ///
    /// [`VaultError::KeyStore`] if Windows will not seal it, [`VaultError::Io`]
    /// if it cannot be written. Both matter: a caller that ignores this has
    /// used up a single-use token and kept no record of its replacement.
    pub fn store(&self, refresh_token: &str) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).map_err(|source| VaultError::Io {
                path: parent.to_path_buf(),
                source,
            })?;
        }
        let sealed = self.sealer.seal(refresh_token.as_bytes())?;
        write_atomically(&self.path, &sealed)
    }

    /// Forget the session.
    ///
    /// Local only. Signing out properly also revokes the token server-side; if
    /// that call fails the token is still removed here, because leaving a
    /// credential on a machine somebody asked to sign out of is the worse of
    /// the two outcomes.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the file exists and cannot be removed.
    pub fn forget(&self) -> Result<()> {
        match std::fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(VaultError::Io {
                path: self.path.clone(),
                source,
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::CredentialStore;
    use crate::seal::PassthroughSealer;

    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-cred-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("creatable");
        dir
    }

    #[test]
    fn a_token_round_trips() {
        let dir = scratch("roundtrip");
        let store = CredentialStore::new(dir.join("session"), PassthroughSealer);

        assert_eq!(store.refresh_token().expect("reads"), None);
        store.store("rt-abc123").expect("stores");
        assert_eq!(
            store.refresh_token().expect("reads"),
            Some("rt-abc123".to_owned())
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Rotation: the new token replaces the old one and nothing keeps a copy.
    /// A stale copy presented later would revoke the whole family.
    #[test]
    fn storing_replaces_rather_than_appends() {
        let dir = scratch("rotate");
        let store = CredentialStore::new(dir.join("session"), PassthroughSealer);

        store.store("rt-first").expect("stores");
        store.store("rt-second").expect("rotates");

        assert_eq!(
            store.refresh_token().expect("reads"),
            Some("rt-second".to_owned())
        );
        let on_disk = std::fs::read(dir.join("session")).expect("reads");
        assert!(
            !String::from_utf8_lossy(&on_disk).contains("rt-first"),
            "the previous token survived the rotation"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn signing_out_leaves_nothing_behind() {
        let dir = scratch("forget");
        let store = CredentialStore::new(dir.join("session"), PassthroughSealer);
        store.store("rt-abc123").expect("stores");

        store.forget().expect("forgets");
        assert_eq!(store.refresh_token().expect("reads"), None);
        assert!(!dir.join("session").exists());
        store.forget().expect("forgetting twice is fine");

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A file this Windows user cannot open means "not signed in here", not an
    /// error nobody can act on.
    #[test]
    fn a_token_this_machine_cannot_open_reads_as_absent() {
        let dir = scratch("foreign");
        let path = dir.join("session");

        struct Refusing;
        impl std::fmt::Debug for Refusing {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("Refusing")
            }
        }
        impl crate::seal::Sealer for Refusing {
            fn seal(&self, plaintext: &[u8]) -> crate::Result<Vec<u8>> {
                Ok(plaintext.to_vec())
            }
            fn unseal(&self, _sealed: &[u8]) -> crate::Result<Vec<u8>> {
                Err(crate::VaultError::KeyStore)
            }
        }

        std::fs::write(&path, b"somebody else's blob").expect("writes");
        let store = CredentialStore::new(&path, Refusing);
        assert_eq!(store.refresh_token().expect("does not fail"), None);

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The real sealer, so the token is not sitting there in plain text.
    #[cfg(windows)]
    #[test]
    fn the_token_is_not_readable_on_disk() {
        use crate::seal::DpapiSealer;

        let dir = scratch("sealed");
        let path = dir.join("session");
        let store = CredentialStore::new(&path, DpapiSealer);
        store.store("rt-secret-value").expect("stores");

        let on_disk = std::fs::read(&path).expect("reads");
        assert!(
            !String::from_utf8_lossy(&on_disk).contains("rt-secret-value"),
            "the refresh token is on the disk in the clear"
        );
        assert_eq!(
            store.refresh_token().expect("reads"),
            Some("rt-secret-value".to_owned())
        );

        let _ = std::fs::remove_dir_all(&dir);
    }
}
