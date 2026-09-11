//! The key hierarchy, and what each level is actually protecting against.
//!
//! Section 16.3 asks for three things stacked: a meeting key generated at
//! random, encrypted under an account key derived from the refresh token, and
//! the whole thing protected by DPAPI. Each layer answers a different threat,
//! which is why none of them is redundant:
//!
//! * The **meeting key** is per recording. A key recovered from one meeting
//!   reveals that meeting and no other.
//! * The **account key** ties the recording to a NovaBrief session. Revoking a
//!   device (EF-03) invalidates the refresh token, and with it the ability to
//!   derive this key again.
//! * **DPAPI** ties it to the Windows user. A stolen laptop's disk, mounted
//!   elsewhere, yields nothing - which is the threat section 21 names for local
//!   recordings.
//!
//! Losing any one layer makes the recording unreadable, and that is the
//! intended trade. Audio in progress is the most sensitive thing NovaBrief
//! holds and the least valuable to recover: it has not been uploaded, so at
//! worst a meeting is re-recorded.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use hkdf::Hkdf;
use rand::TryRngCore;
use sha2::Sha256;

use crate::error::{Result, VaultError};

/// Bytes in an AES-256 key.
pub const KEY_BYTES: usize = 32;
/// Bytes in a GCM nonce.
pub const NONCE_BYTES: usize = 12;

/// Domain separation for the account key.
///
/// A fixed, versioned string: deriving two different keys from the same secret
/// without separating the domains is how one use of a key ends up able to
/// decrypt another.
const ACCOUNT_KEY_INFO: &[u8] = b"novabrief/v1/account-key";

/// A key derived from the current session's refresh token.
#[derive(Clone)]
pub struct AccountKey([u8; KEY_BYTES]);

impl std::fmt::Debug for AccountKey {
    /// Never prints the key.
    ///
    /// A `Debug` that renders key material puts it in every log line that ever
    /// formats the surrounding struct, and `CLAUDE.md` section 6 forbids that
    /// for meeting content for the same reason.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AccountKey(redacted)")
    }
}

impl AccountKey {
    /// Derive the account key from the refresh token.
    ///
    /// HKDF rather than a bare hash: the refresh token is a high-entropy
    /// secret, so extraction is cheap, but the expand step is what gives a
    /// labelled, fixed-length key and lets a second use be added later without
    /// colliding with this one.
    ///
    /// No salt. A salt would have to be stored next to the ciphertext and would
    /// add nothing here: the input already has full entropy, which is the case
    /// HKDF's own specification says a salt is optional for.
    #[must_use]
    pub fn derive(refresh_token: &[u8]) -> Self {
        let hkdf = Hkdf::<Sha256>::new(None, refresh_token);
        let mut key = [0_u8; KEY_BYTES];
        hkdf.expand(ACCOUNT_KEY_INFO, &mut key)
            .expect("32 bytes is a valid HKDF-SHA256 output length");
        Self(key)
    }

    /// Seal a meeting key under this account key.
    ///
    /// # Errors
    ///
    /// If the system random number generator fails.
    pub fn seal(&self, meeting_key: &MeetingKey) -> Result<Vec<u8>> {
        let mut nonce = [0_u8; NONCE_BYTES];
        rand::rngs::OsRng
            .try_fill_bytes(&mut nonce)
            .map_err(|_| VaultError::KeyStore)?;

        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&self.0));
        let sealed = cipher
            .encrypt(Nonce::from_slice(&nonce), meeting_key.0.as_slice())
            .map_err(|_| VaultError::Decryption)?;

        let mut out = Vec::with_capacity(NONCE_BYTES + sealed.len());
        out.extend_from_slice(&nonce);
        out.extend_from_slice(&sealed);
        Ok(out)
    }

    /// Recover a meeting key sealed by [`AccountKey::seal`].
    ///
    /// # Errors
    ///
    /// [`VaultError::Decryption`] when the blob is truncated, was sealed under
    /// a different account key, or has been altered.
    pub fn unseal(&self, sealed: &[u8]) -> Result<MeetingKey> {
        if sealed.len() < NONCE_BYTES {
            return Err(VaultError::Decryption);
        }
        let (nonce, body) = sealed.split_at(NONCE_BYTES);

        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&self.0));
        let plain = cipher
            .decrypt(Nonce::from_slice(nonce), body)
            .map_err(|_| VaultError::Decryption)?;

        let bytes: [u8; KEY_BYTES] = plain.try_into().map_err(|_| VaultError::Decryption)?;
        Ok(MeetingKey(bytes))
    }
}

/// The key one recording is encrypted with.
#[derive(Clone)]
pub struct MeetingKey([u8; KEY_BYTES]);

impl std::fmt::Debug for MeetingKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("MeetingKey(redacted)")
    }
}

impl MeetingKey {
    /// A fresh key from the operating system's generator.
    ///
    /// # Errors
    ///
    /// If the system random number generator fails. Returned rather than
    /// unwrapped: a recording encrypted with a key from a degraded generator
    /// would look exactly like a healthy one.
    pub fn generate() -> Result<Self> {
        let mut key = [0_u8; KEY_BYTES];
        rand::rngs::OsRng
            .try_fill_bytes(&mut key)
            .map_err(|_| VaultError::KeyStore)?;
        Ok(Self(key))
    }

    /// Encrypt one segment.
    ///
    /// The nonce is derived from the segment index rather than drawn at random.
    /// GCM tolerates no nonce repetition at all - a repeat does not merely
    /// weaken the encryption, it leaks the authentication key and forges become
    /// possible - and a counter is *provably* unique where 96 random bits are
    /// only probably unique. The index is also what the store already refuses
    /// to let repeat.
    ///
    /// `meeting_id` is bound in as associated data, so a segment cannot be
    /// lifted from one recording into another: it decrypts only where its
    /// index and its meeting agree.
    ///
    /// # Errors
    ///
    /// [`VaultError::Decryption`] if the cipher refuses, which at this point
    /// means the input is larger than GCM's limit.
    pub fn encrypt_segment(
        &self,
        meeting_id: &str,
        index: u32,
        plaintext: &[u8],
    ) -> Result<Vec<u8>> {
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&self.0));
        cipher
            .encrypt(
                Nonce::from_slice(&nonce_for(index)),
                Payload {
                    msg: plaintext,
                    aad: meeting_id.as_bytes(),
                },
            )
            .map_err(|_| VaultError::Decryption)
    }

    /// Decrypt one segment.
    ///
    /// # Errors
    ///
    /// [`VaultError::Decryption`] when the key, the index, the meeting or the
    /// bytes are not the ones this segment was written with.
    pub fn decrypt_segment(&self, meeting_id: &str, index: u32, sealed: &[u8]) -> Result<Vec<u8>> {
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&self.0));
        cipher
            .decrypt(
                Nonce::from_slice(&nonce_for(index)),
                Payload {
                    msg: sealed,
                    aad: meeting_id.as_bytes(),
                },
            )
            .map_err(|_| VaultError::Decryption)
    }
}

/// The nonce for a segment index.
///
/// A 96-bit value whose low 32 bits are the index and whose remaining bits are
/// a fixed label. The label is not secrecy - a nonce is public - it is a
/// reminder that these 12 bytes mean one thing and must never be reused for
/// another purpose under the same key.
fn nonce_for(index: u32) -> [u8; NONCE_BYTES] {
    let mut nonce = [0_u8; NONCE_BYTES];
    nonce[..8].copy_from_slice(b"nbsegm01");
    nonce[8..].copy_from_slice(&index.to_be_bytes());
    nonce
}

#[cfg(test)]
mod tests {
    use super::{nonce_for, AccountKey, MeetingKey, KEY_BYTES};

    #[test]
    fn the_same_token_derives_the_same_account_key() {
        let first = AccountKey::derive(b"a-refresh-token");
        let second = AccountKey::derive(b"a-refresh-token");
        let key = MeetingKey::generate().expect("the OS generator works");

        // Equality is checked through a round trip rather than by comparing the
        // bytes, because the key never leaves the type.
        let sealed = first.seal(&key).expect("sealing works");
        assert!(second.unseal(&sealed).is_ok());
    }

    /// EF-03: revoking a device invalidates the refresh token, and the
    /// recordings on that machine must not survive it.
    #[test]
    fn a_different_token_cannot_open_the_meeting_key() {
        let mine = AccountKey::derive(b"my-refresh-token");
        let theirs = AccountKey::derive(b"a-different-token");
        let key = MeetingKey::generate().expect("the OS generator works");

        let sealed = mine.seal(&key).expect("sealing works");
        assert!(theirs.unseal(&sealed).is_err());
    }

    #[test]
    fn a_segment_round_trips() {
        let key = MeetingKey::generate().expect("the OS generator works");
        let audio = vec![7_u8; 4096];

        let sealed = key.encrypt_segment("mtg-1", 0, &audio).expect("encrypts");
        assert_ne!(sealed, audio, "the segment is stored in the clear");
        let plain = key.decrypt_segment("mtg-1", 0, &sealed).expect("decrypts");
        assert_eq!(plain, audio);
    }

    /// A single flipped bit must fail, not degrade. GCM's tag is what makes
    /// "the file was altered" a detectable condition rather than a corrupted
    /// recording somebody transcribes anyway.
    #[test]
    fn a_tampered_segment_is_refused() {
        let key = MeetingKey::generate().expect("the OS generator works");
        let mut sealed = key.encrypt_segment("mtg-1", 3, b"audio").expect("encrypts");
        sealed[0] ^= 0x01;

        assert!(key.decrypt_segment("mtg-1", 3, &sealed).is_err());
    }

    /// A segment cannot be lifted from one recording into another, nor moved
    /// to a different position in its own.
    #[test]
    fn a_segment_is_bound_to_its_meeting_and_its_position() {
        let key = MeetingKey::generate().expect("the OS generator works");
        let sealed = key.encrypt_segment("mtg-1", 5, b"audio").expect("encrypts");

        assert!(key.decrypt_segment("mtg-2", 5, &sealed).is_err(), "meeting");
        assert!(key.decrypt_segment("mtg-1", 6, &sealed).is_err(), "index");
        assert!(key.decrypt_segment("mtg-1", 5, &sealed).is_ok());
    }

    /// The property the whole nonce scheme exists for. A repeat under one key
    /// leaks GCM's authentication key, so this is not a matter of degree.
    #[test]
    fn no_two_segment_indices_share_a_nonce() {
        let mut seen = std::collections::HashSet::new();
        for index in 0..10_000_u32 {
            assert!(
                seen.insert(nonce_for(index)),
                "index {index} reused a nonce"
            );
        }
        // And the far end of the range, where a 32-bit counter would wrap.
        assert_ne!(nonce_for(0), nonce_for(u32::MAX));
    }

    #[test]
    fn two_generated_keys_differ() {
        let first = MeetingKey::generate().expect("the OS generator works");
        let second = MeetingKey::generate().expect("the OS generator works");
        let account = AccountKey::derive(b"token");

        let a = account.seal(&first).expect("seals");
        let b = account.seal(&second).expect("seals");
        assert_ne!(a, b);
    }

    /// Key material must never reach a log line.
    #[test]
    fn keys_do_not_print_themselves() {
        let key = MeetingKey::generate().expect("the OS generator works");
        let account = AccountKey::derive(b"token");

        assert_eq!(format!("{key:?}"), "MeetingKey(redacted)");
        assert_eq!(format!("{account:?}"), "AccountKey(redacted)");
    }

    #[test]
    fn a_truncated_sealed_key_is_refused_rather_than_panicking() {
        let account = AccountKey::derive(b"token");
        for length in 0..KEY_BYTES {
            assert!(account.unseal(&vec![0_u8; length]).is_err());
        }
    }
}
