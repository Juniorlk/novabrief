//! Where NovaBrief keeps things on a Windows machine.
//!
//! `%LOCALAPPDATA%`, not `%APPDATA%`. Roaming profiles are copied to a file
//! server at every sign-out, and a meeting in progress is tens of megabytes of
//! audio that belongs to this machine and to no other - a recording that
//! roamed would double the network cost of every meeting and arrive on a
//! second machine where DPAPI cannot open it anyway.

use std::path::{Path, PathBuf};

/// The root of everything this installation owns.
///
/// # Errors
///
/// [`std::io::Error`] if `%LOCALAPPDATA%` is not set, or the directory cannot
/// be created. Neither is recoverable here: the application has nowhere to
/// record to, and saying so at start-up is better than discovering it when
/// somebody presses Record.
pub fn data_root() -> std::io::Result<PathBuf> {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or_else(|| std::io::Error::other("LOCALAPPDATA is not set"))?;
    let root = base.join("NovaBrief");
    std::fs::create_dir_all(&root)?;
    Ok(root)
}

/// Where the encrypted recordings live.
#[must_use]
pub fn recordings_root(data_root: &Path) -> PathBuf {
    data_root.join("recordings")
}

/// The device secret that the vault's account key is derived from.
///
/// Beside the recordings rather than inside them: purging a meeting must never
/// be able to take the key that opens the others with it.
#[must_use]
pub fn device_key_path(data_root: &Path) -> PathBuf {
    data_root.join("device.key")
}

/// The sealed refresh token (EF-11).
#[must_use]
pub fn credentials_path(data_root: &Path) -> PathBuf {
    data_root.join("credentials.bin")
}
