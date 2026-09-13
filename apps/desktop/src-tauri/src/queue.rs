//! The thing that empties the vault into the server (EF-18).
//!
//! The `uploader` crate sends one recording once and has no timer in it, on
//! purpose. This is where the timer lives, and where "retry for ever, backing
//! off to five minutes" is a single visible loop rather than a policy spread
//! across call sites.
//!
//! What it works from is **the disk, not a list in memory**. Every recording
//! the vault holds in `Uploading` is owed an upload, whoever recorded it and
//! whichever process did: the meeting the laptop died in the middle of is
//! picked up by the next start without anybody asking.

use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use tokio::sync::{mpsc, Mutex};
use uploader::{Backoff, Job, Sent, Transport, Uploader};
use vault::{AccountKey, DpapiSealer, RecordingState, Vault};

use crate::auth::{Account, Identity};

/// How often the queue looks at the vault when nothing has woken it.
///
/// A backstop, not the mechanism: finishing a recording nudges the queue
/// directly. This is what covers the cases nothing nudges - a meeting left
/// behind by a crash, or a network that came back while the application sat
/// idle.
const SWEEP: Duration = Duration::from_secs(60);

/// What one recording in the queue is doing.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Pending {
    /// The vault directory it lives in.
    pub meeting_id: String,
    /// Audio kept, in milliseconds.
    pub recorded_ms: u64,
    /// Parts the store has confirmed.
    pub parts_done: u32,
    /// Parts in total, or zero before the first ticket.
    pub parts_total: u32,
    /// Failed attempts so far.
    pub attempts: u32,
    /// What stopped the last attempt, if one has been stopped.
    ///
    /// Shown so the person can tell "no network" from "sign in again", which
    /// are the two cases with different answers.
    pub last_error: Option<String>,
    /// Whether this one has been given up on.
    ///
    /// Only for faults that cannot pass - a recording that will not decrypt.
    /// A network failure never lands here, because there is no number of
    /// failures at which a recorded meeting stops being worth sending.
    pub abandoned: bool,
}

/// The upload queue, as the interface sees it.
#[derive(Debug, Default)]
pub struct Status {
    /// One entry per recording that still owes an upload.
    pub pending: Vec<Pending>,
}

/// Drives uploads in the background.
#[derive(Debug)]
pub struct Queue {
    status: Arc<Mutex<Status>>,
    wake: mpsc::Sender<()>,
}

impl Queue {
    /// Start the loop, and hand back the handle that talks to it.
    ///
    /// The account and the transport are shared: the same client serves the
    /// session and the upload, so a renewal made for one is seen by the other.
    pub fn start<I, T>(
        account: Arc<Account<I>>,
        transport: Arc<T>,
        root: std::path::PathBuf,
        key: AccountKey,
    ) -> Self
    where
        I: Identity + Send + Sync + 'static,
        T: Transport + Send + Sync + 'static,
    {
        let status = Arc::new(Mutex::new(Status::default()));
        let (wake, mut woken) = mpsc::channel::<()>(8);

        let worker = Arc::clone(&status);
        tauri::async_runtime::spawn(async move {
            let uploader = Uploader::new(transport.as_ref());
            let mut backoff = Backoff::new();
            loop {
                let waited = match sweep(&uploader, &account, &root, &key, &worker).await {
                    Swept::Nothing | Swept::Everything => {
                        backoff.reset();
                        SWEEP
                    }
                    // Something is still owed and the last attempt failed, so
                    // the next one waits - and the wait grows, because a
                    // client that hammers a server which is down is part of
                    // why it is down.
                    Swept::Stalled => backoff.wait(),
                };

                // Either the timer or somebody finishing a recording, and a
                // nudge that arrives during an attempt is not lost: the
                // channel holds it until here.
                tokio::select! {
                    () = tokio::time::sleep(waited) => {}
                    received = woken.recv() => {
                        if received.is_none() {
                            // Nobody can nudge it any more, so the application
                            // is shutting down.
                            return;
                        }
                    }
                }
            }
        });

        Self { status, wake }
    }

    /// Tell the queue there is something new to send.
    ///
    /// Never blocks, and a nudge that finds the channel full is dropped on
    /// purpose: the queue is about to sweep anyway, and a recording is found
    /// by looking at the vault rather than by being told about it.
    pub fn nudge(&self) {
        let _ = self.wake.try_send(());
    }

    /// What the queue is doing, for the interface.
    pub async fn status(&self) -> Vec<Pending> {
        self.status.lock().await.pending.clone()
    }
}

/// What one pass over the vault achieved.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Swept {
    /// Nothing was owed.
    Nothing,
    /// Everything owed was sent.
    Everything,
    /// Something is still owed, and the attempt did not get there.
    Stalled,
}

/// Send everything the vault still owes, once.
///
/// Public because the loop above is a timer and this is the behaviour: which
/// recordings are owed an upload, how many tokens one pass spends, and what a
/// failure leaves behind are all decided here and none of them are worth
/// testing through a sixty-second sleep.
pub async fn sweep<I, T>(
    uploader: &Uploader<&T>,
    account: &Account<I>,
    root: &std::path::Path,
    key: &AccountKey,
    status: &Arc<Mutex<Status>>,
) -> Swept
where
    I: Identity,
    T: Transport + Sync,
{
    let store = Vault::new(root, key.clone(), DpapiSealer);
    let Ok(manifests) = store.list() else {
        return Swept::Nothing;
    };
    let owed: Vec<_> = manifests
        .into_iter()
        .filter(|manifest| manifest.state == RecordingState::Uploading)
        .collect();

    if owed.is_empty() {
        status.lock().await.pending.clear();
        return Swept::Nothing;
    }

    // Once per pass rather than once per recording. Not for safety - renewing
    // is idempotent while the token is live, and the account serialises the
    // renewals that are not - but because the failure has one answer for the
    // whole pass: with no session, nothing here can be sent, and reporting
    // that once is what the interface needs to say "sign in again".
    let token = match account.access_token().await {
        Ok(token) => token,
        Err(message) => {
            let mut guard = status.lock().await;
            guard.pending = owed
                .iter()
                .map(|manifest| Pending {
                    meeting_id: manifest.meeting_id.clone(),
                    recorded_ms: manifest.duration_ms(),
                    parts_done: 0,
                    parts_total: 0,
                    attempts: 0,
                    last_error: Some(message.clone()),
                    abandoned: false,
                })
                .collect();
            return Swept::Stalled;
        }
    };

    let mut stalled = false;
    let mut pending = Vec::new();
    for manifest in owed {
        let Ok(mut recording) = store.reopen(&manifest.meeting_id) else {
            // A recording this machine cannot open is not a network problem
            // and never becomes one. It is reported and left alone.
            pending.push(Pending {
                meeting_id: manifest.meeting_id.clone(),
                recorded_ms: manifest.duration_ms(),
                parts_done: 0,
                parts_total: 0,
                attempts: 0,
                last_error: Some("this recording cannot be opened on this machine".to_owned()),
                abandoned: true,
            });
            continue;
        };

        let job = Job {
            // `None` when the recording was made with no network: the
            // uploader then declares it, once.
            meeting_id: manifest.server_meeting_id.clone(),
            debug_id: Some(manifest.debug_id.clone()),
            started_at: manifest.started_at.clone(),
            title: None,
            recorded_ms: manifest.duration_ms(),
            paused_ms: manifest.paused_ms,
        };

        let seen = Arc::new(std::sync::Mutex::new(Sent {
            parts_done: 0,
            parts_total: 0,
        }));
        let watched = Arc::clone(&seen);
        let mut report = move |sent: Sent| {
            if let Ok(mut last) = watched.lock() {
                *last = sent;
            }
        };

        match uploader
            .attempt(&token, &mut recording, &job, &mut report)
            .await
        {
            Ok(_) => {}
            Err(error) => {
                let transient = error.is_transient();
                stalled |= transient;
                let sent = seen.lock().map(|last| *last).unwrap_or(Sent {
                    parts_done: 0,
                    parts_total: 0,
                });
                pending.push(Pending {
                    meeting_id: manifest.meeting_id.clone(),
                    recorded_ms: manifest.duration_ms(),
                    parts_done: sent.parts_done,
                    parts_total: sent.parts_total,
                    attempts: 1,
                    last_error: Some(error.to_string()),
                    abandoned: !transient,
                });
            }
        }
    }

    status.lock().await.pending.clone_from(&pending);
    if stalled {
        Swept::Stalled
    } else if pending.is_empty() {
        Swept::Everything
    } else {
        // Everything left is abandoned, so waiting longer changes nothing.
        Swept::Nothing
    }
}
