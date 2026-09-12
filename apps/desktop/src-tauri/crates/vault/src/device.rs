//! The secret that identifies this installation.
//!
//! Section 16.3 says the meeting key is "encrypted by an account key derived
//! from the refresh token". Taken literally that does not survive contact with
//! the rest of the system: **the refresh token rotates**. It is single-use, and
//! presenting it twice revokes the whole family (`packages/schemas/auth.py`),
//! so a new one arrives every time the desktop refreshes its session.
//!
//! An account key derived from it would change with it, and every recording
//! sealed under the previous one would become unopenable - including the ones
//! crash recovery exists to rescue, which are by definition the ones nobody
//! managed to upload. A laptop that dies overnight and refreshes its session in
//! the morning would lose exactly the meeting it was meant to recover.
//!
//! So the key derives from a secret that is generated once per installation and
//! never rotated, sealed by DPAPI like everything else here. What that costs is
//! the property that revoking a device makes its local audio unreadable - and
//! that property was never real: a revoked user simply would not run the app,
//! and nothing would reach the disk to be protected. What it keeps is the one
//! that matters, which is that the disk is worthless off this machine.

use std::path::{Path, PathBuf};

use rand::TryRngCore;

use crate::error::{Result, VaultError};
use crate::keys::{AccountKey, KEY_BYTES};
use crate::seal::Sealer;

/// A per-installation secret, from which the account key is derived.
#[derive(Clone)]
pub struct DeviceSecret([u8; KEY_BYTES]);

impl std::fmt::Debug for DeviceSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("DeviceSecret(redacted)")
    }
}

impl DeviceSecret {
    /// Load this installation's secret, creating it on first use.
    ///
    /// Created rather than asked for: there is nothing a person could type that
    /// would be as good, and a secret the user has to remember is a secret that
    /// ends up in a password manager belonging to somebody else.
    ///
    /// # Errors
    ///
    /// [`VaultError::Io`] if the file cannot be read or written,
    /// [`VaultError::KeyStore`] if Windows will not seal or unseal it - which
    /// on an existing file means a different Windows user, and is the stolen
    /// disk case working as intended.
    pub fn load_or_create<S: Sealer>(path: &Path, sealer: &S) -> Result<Self> {
        match std::fs::read(path) {
            Ok(sealed) => {
                let plain = sealer.unseal(&sealed)?;
                let bytes: [u8; KEY_BYTES] = plain.try_into().map_err(|_| VaultError::KeyStore)?;
                Ok(Self(bytes))
            }
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => {
                Self::create(path, sealer)
            }
            Err(source) => Err(VaultError::Io {
                path: path.to_path_buf(),
                source,
            }),
        }
    }

    fn create<S: Sealer>(path: &Path, sealer: &S) -> Result<Self> {
        let mut bytes = [0_u8; KEY_BYTES];
        rand::rngs::OsRng
            .try_fill_bytes(&mut bytes)
            .map_err(|_| VaultError::KeyStore)?;

        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|source| VaultError::Io {
                path: parent.to_path_buf(),
                source,
            })?;
        }
        write_atomically(path, &sealer.seal(&bytes)?)?;
        Ok(Self(bytes))
    }

    /// The key meeting keys are sealed under.
    #[must_use]
    pub fn account_key(&self) -> AccountKey {
        AccountKey::derive(&self.0)
    }
}

/// Replace a file's contents without ever leaving a half-written one behind.
///
/// The same discipline as the manifest, for the same reason: a torn write here
/// costs the device secret, and with it every recording waiting to be uploaded.
pub(crate) fn write_atomically(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write as _;

    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".writing");
    let temporary: PathBuf = path.with_file_name(name);

    let write = || -> std::io::Result<()> {
        let mut file = std::fs::File::create(&temporary)?;
        file.write_all(bytes)?;
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

#[cfg(test)]
mod tests {
    use super::DeviceSecret;
    use crate::seal::PassthroughSealer;

    fn scratch(name: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("nb-device-{name}-{}-{unique}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("creatable");
        dir
    }

    /// The property the whole module exists for: the secret is stable, so a
    /// recording sealed today opens tomorrow whatever the session did in
    /// between.
    #[test]
    fn the_secret_survives_being_loaded_again() {
        let dir = scratch("stable");
        let path = dir.join("device.key");

        let first = DeviceSecret::load_or_create(&path, &PassthroughSealer).expect("creates");
        let second = DeviceSecret::load_or_create(&path, &PassthroughSealer).expect("loads");

        // Compared through what it is for, since the bytes never leave the type.
        let key = crate::keys::MeetingKey::generate().expect("generates");
        let sealed = first.account_key().seal(&key).expect("seals");
        assert!(second.account_key().unseal(&sealed).is_ok());

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Two installations must not share a key.
    #[test]
    fn two_installations_get_different_secrets() {
        let first_dir = scratch("first");
        let second_dir = scratch("second");

        let first = DeviceSecret::load_or_create(&first_dir.join("device.key"), &PassthroughSealer)
            .expect("creates");
        let second =
            DeviceSecret::load_or_create(&second_dir.join("device.key"), &PassthroughSealer)
                .expect("creates");

        let key = crate::keys::MeetingKey::generate().expect("generates");
        let sealed = first.account_key().seal(&key).expect("seals");
        assert!(
            second.account_key().unseal(&sealed).is_err(),
            "two installations derived the same key"
        );

        let _ = std::fs::remove_dir_all(&first_dir);
        let _ = std::fs::remove_dir_all(&second_dir);
    }

    #[test]
    fn the_secret_is_not_stored_in_the_clear_by_the_real_sealer() {
        #[cfg(windows)]
        {
            use crate::seal::DpapiSealer;
            let dir = scratch("sealed");
            let path = dir.join("device.key");
            DeviceSecret::load_or_create(&path, &DpapiSealer).expect("creates");

            let on_disk = std::fs::read(&path).expect("reads");
            assert!(on_disk.len() > 32, "that is too short to be a DPAPI blob");
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    #[test]
    fn a_corrupted_secret_is_an_error_not_a_new_one() {
        let dir = scratch("corrupt");
        let path = dir.join("device.key");
        // A file that is not the right length: silently replacing it would
        // orphan every recording sealed under the real secret without saying so.
        std::fs::write(&path, b"too short").expect("writes");

        assert!(DeviceSecret::load_or_create(&path, &PassthroughSealer).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_secret_does_not_print_itself() {
        let dir = scratch("debug");
        let secret =
            DeviceSecret::load_or_create(&dir.join("device.key"), &PassthroughSealer).expect("ok");
        assert_eq!(format!("{secret:?}"), "DeviceSecret(redacted)");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
