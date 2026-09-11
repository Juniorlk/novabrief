//! What can go wrong in the vault, and how much of it we are willing to say.

use std::path::PathBuf;

/// A vault operation failed.
#[derive(Debug, thiserror::Error)]
pub enum VaultError {
    /// The filesystem refused.
    #[error("{path}: {source}")]
    Io {
        /// What we were touching.
        path: PathBuf,
        /// Why it refused.
        source: std::io::Error,
    },

    /// A segment or a key could not be decrypted.
    ///
    /// Deliberately says nothing about *why*. The distinction between "wrong
    /// key" and "tampered ciphertext" is exactly what an attacker with the file
    /// would like to learn, and nobody legitimate needs it: for the person
    /// running NovaBrief both mean the same thing, which is that this recording
    /// cannot be read on this machine any more.
    #[error("this recording cannot be decrypted")]
    Decryption,

    /// The manifest on disk is not one we can read.
    #[error("the manifest is unreadable: {0}")]
    Manifest(String),

    /// Windows refused to protect or unprotect the key material.
    #[error("the Windows key store refused the operation")]
    KeyStore,

    /// A segment index was written twice, or out of order.
    ///
    /// Loud rather than tolerated: the segment index is what makes the AES-GCM
    /// nonces unique, and a repeat would reuse one. Nonce reuse in GCM does not
    /// merely weaken the encryption, it leaks the authentication key.
    #[error("segment {got} was offered after segment {expected} - indices must not repeat")]
    SegmentOutOfOrder {
        /// The index that was offered.
        got: u32,
        /// The index the vault expected next.
        expected: u32,
    },
}

/// The result of a vault operation.
pub type Result<T> = std::result::Result<T, VaultError>;
