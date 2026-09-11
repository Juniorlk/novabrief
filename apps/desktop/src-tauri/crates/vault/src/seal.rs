//! DPAPI: tying the key to this Windows user.
//!
//! The layer that answers the stolen-laptop threat of section 21.3. The disk
//! can be pulled and mounted anywhere; what DPAPI protects comes back only on
//! the same machine, for the same user account.
//!
//! Behind a trait because the crypto above it deserves tests that do not need
//! an operating system to agree, and because one day there will be a macOS
//! build. The test double is `#[cfg(test)]` on purpose: a passthrough sealer
//! compiled into a release is a null-encryption path waiting to be selected by
//! a configuration mistake.

use crate::error::{Result, VaultError};

/// Something that can protect a small secret at rest.
pub trait Sealer: std::fmt::Debug {
    /// Protect `plaintext`.
    ///
    /// # Errors
    ///
    /// [`VaultError::KeyStore`] when the platform refuses.
    fn seal(&self, plaintext: &[u8]) -> Result<Vec<u8>>;

    /// Recover what [`Sealer::seal`] protected.
    ///
    /// # Errors
    ///
    /// [`VaultError::KeyStore`] when the platform refuses - a different user,
    /// a different machine, or an altered blob.
    fn unseal(&self, sealed: &[u8]) -> Result<Vec<u8>>;
}

/// Windows Data Protection API.
#[cfg(windows)]
#[derive(Debug, Clone, Copy, Default)]
pub struct DpapiSealer;

#[cfg(windows)]
impl Sealer for DpapiSealer {
    fn seal(&self, plaintext: &[u8]) -> Result<Vec<u8>> {
        use windows::Win32::Foundation::LocalFree;
        use windows::Win32::Security::Cryptography::{
            CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
        };

        let mut input = CRYPT_INTEGER_BLOB {
            cbData: u32::try_from(plaintext.len()).map_err(|_| VaultError::KeyStore)?,
            pbData: plaintext.as_ptr().cast_mut(),
        };
        let mut output = CRYPT_INTEGER_BLOB::default();

        // SAFETY: both blobs are live locals, `input` points at a slice that
        // outlives the call, and the output blob is freed below whatever
        // happens.
        unsafe {
            CryptProtectData(
                &raw mut input,
                None,
                None,
                None,
                None,
                // No interactive prompt: this runs while a meeting is being
                // recorded, and a dialog appearing over somebody's call is not
                // an acceptable failure mode.
                CRYPTPROTECT_UI_FORBIDDEN,
                &raw mut output,
            )
            .map_err(|_| VaultError::KeyStore)?;

            let sealed = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
            let _ = LocalFree(Some(windows::Win32::Foundation::HLOCAL(
                output.pbData.cast(),
            )));
            Ok(sealed)
        }
    }

    fn unseal(&self, sealed: &[u8]) -> Result<Vec<u8>> {
        use windows::Win32::Foundation::LocalFree;
        use windows::Win32::Security::Cryptography::{
            CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
        };

        let mut input = CRYPT_INTEGER_BLOB {
            cbData: u32::try_from(sealed.len()).map_err(|_| VaultError::KeyStore)?,
            pbData: sealed.as_ptr().cast_mut(),
        };
        let mut output = CRYPT_INTEGER_BLOB::default();

        // SAFETY: as above.
        unsafe {
            CryptUnprotectData(
                &raw mut input,
                None,
                None,
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                &raw mut output,
            )
            .map_err(|_| VaultError::KeyStore)?;

            let plain = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
            let _ = LocalFree(Some(windows::Win32::Foundation::HLOCAL(
                output.pbData.cast(),
            )));
            Ok(plain)
        }
    }
}

/// A sealer that protects nothing, for tests only.
///
/// Never compiled into a release: see the module documentation.
#[cfg(test)]
#[derive(Debug, Clone, Copy, Default)]
pub struct PassthroughSealer;

#[cfg(test)]
impl Sealer for PassthroughSealer {
    fn seal(&self, plaintext: &[u8]) -> Result<Vec<u8>> {
        Ok(plaintext.to_vec())
    }

    fn unseal(&self, sealed: &[u8]) -> Result<Vec<u8>> {
        Ok(sealed.to_vec())
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::{DpapiSealer, Sealer};

    #[test]
    fn dpapi_round_trips() {
        let sealer = DpapiSealer;
        let secret = b"a sealed meeting key, 32 bytes..";

        let sealed = sealer.seal(secret).expect("Windows protects it");
        assert_ne!(sealed.as_slice(), secret.as_slice(), "stored in the clear");
        assert_eq!(sealer.unseal(&sealed).expect("and gives it back"), secret);
    }

    /// The tag is what turns "somebody edited the file" into a refusal rather
    /// than a key that is subtly wrong.
    #[test]
    fn an_altered_blob_is_refused() {
        let sealer = DpapiSealer;
        let mut sealed = sealer.seal(b"secret").expect("protects");
        let last = sealed.len() - 1;
        sealed[last] ^= 0xff;

        assert!(sealer.unseal(&sealed).is_err());
    }

    #[test]
    fn an_empty_blob_is_refused_rather_than_panicking() {
        assert!(DpapiSealer.unseal(&[]).is_err());
    }
}
