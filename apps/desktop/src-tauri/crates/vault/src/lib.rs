//! Encrypted local storage for recordings in progress (EF-17, section 16.3).
//!
//! NovaBrief records to the disk first and uploads afterwards, because ADR-05
//! says a network outage must never cost audio. That makes the laptop, not the
//! server, the place where a meeting is most exposed: it is sitting in plain
//! reach of whoever has the machine, and it has not been anywhere else yet.
//!
//! Three properties, and each is tested rather than asserted:
//!
//! * **Nothing readable reaches the disk.** Segments are AES-256-GCM, under a
//!   key that exists only for this meeting, this Windows user and this
//!   NovaBrief session.
//! * **A power cut costs five seconds, not a meeting.** Segments are flushed
//!   before the manifest names them, and the manifest is replaced by rename
//!   rather than rewritten in place.
//! * **A crash is visible afterwards.** A recording exists on disk from before
//!   its first segment, so start-up can find it and say what happened.

pub mod credentials;
pub mod device;
pub mod error;
pub mod keys;
pub mod manifest;
pub mod seal;
pub mod store;

pub use credentials::CredentialStore;
pub use device::DeviceSecret;
pub use error::{Result, VaultError};
pub use keys::{AccountKey, MeetingKey};
pub use manifest::{Manifest, RecordingState, SegmentRecord};
pub use seal::Sealer;
pub use store::{Recording, Vault};

#[cfg(windows)]
pub use seal::DpapiSealer;
