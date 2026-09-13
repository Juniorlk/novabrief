//! What one pass of the upload queue does, and what it leaves alone.
//!
//! The loop around it is a timer and is not interesting. These are:
//!
//! * a recording the server already has must not be sent again - the customer
//!   would pay twice and get the same meeting twice;
//! * a dead network must leave the recording owed rather than give it away;
//! * a recording this machine cannot open must not be retried for ever, or it
//!   holds up every meeting behind it and never says why.
//!
//! None of them is visible from the outside until it has already cost
//! something.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use api_client::{ApiError, Meeting, Profile, TokenPair, UploadSlot, UploadTicket, UploadedPart};
use novabrief_desktop_lib::auth::{Account, Identity};
use novabrief_desktop_lib::queue::{sweep, Status, Swept};
use tokio::sync::Mutex as AsyncMutex;
use uploader::{Transport, Uploader};
use vault::{AccountKey, CredentialStore, DpapiSealer, RecordingState, Vault};

fn scratch(name: &str) -> PathBuf {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let unique = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("nb-q-{name}-{}-{unique}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("a scratch directory");
    dir
}

fn key() -> AccountKey {
    AccountKey::derive(b"a-device-secret")
}

/// A recording of one small segment, left in `state`.
fn record(store: &Vault<DpapiSealer>, id: &str, state: RecordingState) {
    let mut recording = store
        .begin(id, &format!("DBG-{id}"), "mic", "speakers")
        .expect("the vault opens");
    recording
        .note_start("2026-09-13T10:00:00Z")
        .expect("the start is written");
    recording
        .append(0, &vec![7_u8; 4096], 5_000)
        .expect("a segment is stored");
    recording.set_state(state).expect("the state is written");
}

#[derive(Debug, Default)]
struct Seen {
    /// Sessions renewed.
    renewals: u32,
    /// Meetings that were declared.
    declared: u32,
    /// Uploads opened, by the meeting they were opened for.
    started: Vec<String>,
    /// Meetings finalised.
    finalized: Vec<String>,
    /// The store refuses every part, as a dead network would.
    network_is_down: bool,
}

/// The server, the store and the authentication endpoints, all at once.
#[derive(Debug, Default)]
struct Server {
    seen: Mutex<Seen>,
}

impl Server {
    fn seen(&self) -> std::sync::MutexGuard<'_, Seen> {
        self.seen.lock().expect("the double is not poisoned")
    }
}

fn meeting(id: &str, status: &str) -> Meeting {
    serde_json::from_value(serde_json::json!({
        "id": id,
        "debug_id": "DBG-SRV",
        "status": status,
        "title": null,
        "failed_reason": null,
        "duration_seconds": 5,
    }))
    .expect("the double answers the contract")
}

impl Identity for Server {
    async fn sign_in(&self, _email: &str, _password: &str) -> Result<TokenPair, ApiError> {
        {
            Ok(TokenPair {
                access_token: "access-1".to_owned(),
                refresh_token: "refresh-1".to_owned(),
                // Already expired, so a pass has to renew before it can send
                // anything - which is the ordering the queue depends on.
                expires_in: 0,
            })
        }
    }

    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        let presented = refresh_token.to_owned();
        async move {
            tokio::task::yield_now().await;
            let mut seen = self.seen();
            seen.renewals += 1;
            let n = seen.renewals;
            let _ = presented;
            Ok(TokenPair {
                access_token: format!("access-{n}"),
                refresh_token: format!("refresh-{n}"),
                expires_in: 3600,
            })
        }
    }

    async fn profile(&self, _access_token: &str) -> Result<Profile, ApiError> {
        {
            Ok(Profile {
                full_name: "Awa Ndiaye".to_owned(),
                email: None,
                role: "Owner".to_owned(),
                locale: "fr".to_owned(),
                email_verified: true,
            })
        }
    }
}

impl Transport for Server {
    fn declare_meeting(
        &self,
        _access_token: &str,
        _started_at: &str,
        _title: Option<&str>,
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        let id = {
            let mut seen = self.seen();
            seen.declared += 1;
            format!("srv-{}", seen.declared)
        };
        async move { Ok(meeting(&id, "CREATED")) }
    }

    fn start_upload(
        &self,
        _access_token: &str,
        meeting_id: &str,
        _size_bytes: u64,
        _sha256: &str,
        _duration_seconds: u64,
        _paused_seconds: u64,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        self.seen().started.push(meeting_id.to_owned());
        async {
            Ok(UploadTicket {
                upload_id: "upload-1".to_owned(),
                part_size_bytes: 5 * 1024 * 1024,
                parts: vec![UploadSlot {
                    part_number: 1,
                    url: "https://store.invalid/audio.ogg?part=1".to_owned(),
                }],
                expires_in_seconds: 900,
            })
        }
    }

    fn resume_upload(
        &self,
        _access_token: &str,
        _meeting_id: &str,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        self.start_upload("", "", 0, "", 0, 0)
    }

    fn put_part(
        &self,
        _url: &str,
        _bytes: Vec<u8>,
    ) -> impl std::future::Future<Output = Result<String, ApiError>> + Send {
        let down = self.seen().network_is_down;
        async move {
            if down {
                Err(ApiError::Unreachable)
            } else {
                Ok("etag-1".to_owned())
            }
        }
    }

    fn finalize(
        &self,
        _access_token: &str,
        meeting_id: &str,
        _upload_id: &str,
        _parts: &[UploadedPart],
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        self.seen().finalized.push(meeting_id.to_owned());
        let id = meeting_id.to_owned();
        async move { Ok(meeting(&id, "QUEUED")) }
    }
}

/// An account already signed in against the double.
async fn signed_in<'a>(server: &'a Server, root: &Path) -> Account<&'a Server> {
    let store = CredentialStore::new(root.join("credentials.bin"), DpapiSealer);
    let account = Account::new(server, store);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");
    account
}

/// One pass, and everything it touched.
async fn pass(server: &Server, root: &Path, account: &Account<&Server>) -> (Swept, Vec<String>) {
    let uploader = Uploader::new(server);
    let status = Arc::new(AsyncMutex::new(Status::default()));
    let swept = sweep(&uploader, account, root, &key(), &status).await;
    let pending = status
        .lock()
        .await
        .pending
        .iter()
        .map(|entry| entry.meeting_id.clone())
        .collect();
    (swept, pending)
}

/// The queue works from the disk: everything the vault holds in `Uploading` is
/// owed an upload, and nothing else is.
#[tokio::test]
async fn only_the_recordings_that_owe_an_upload_are_sent() {
    let root = scratch("owed");
    let store = Vault::new(&root, key(), DpapiSealer);
    record(&store, "waiting-one", RecordingState::Uploading);
    record(&store, "waiting-two", RecordingState::Uploading);
    // Already with the server. Sending it again would bill the customer twice
    // and give them the same meeting twice.
    record(&store, "already-sent", RecordingState::Uploaded);
    // Still being written, as a crash leaves one.
    record(&store, "interrupted", RecordingState::Interrupted);

    let server = Server::default();
    let account = signed_in(&server, &root).await;
    let (swept, pending) = pass(&server, &root, &account).await;

    assert_eq!(swept, Swept::Everything);
    assert!(pending.is_empty(), "something is still owed: {pending:?}");

    let seen = server.seen();
    let mut sent = seen.finalized.clone();
    sent.sort();
    assert_eq!(sent.len(), 2, "expected two uploads, got {sent:?}");
    assert_eq!(
        seen.declared, 2,
        "a recording made offline is declared once, at upload time"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A recording this machine cannot open is reported and let alone.
///
/// Not a network problem and never becomes one - it means a different Windows
/// account, or a device secret that has changed. Retrying it every five
/// minutes for ever would hide a real fault behind a queue that never empties
/// and never complains, and would keep every meeting behind it waiting.
#[tokio::test]
async fn a_recording_that_cannot_be_opened_is_not_retried_for_ever() {
    let root = scratch("unreadable");
    // Written under one key, and the queue will come with another.
    let theirs = Vault::new(&root, AccountKey::derive(b"another-machine"), DpapiSealer);
    record(&theirs, "not-mine", RecordingState::Uploading);

    let server = Server::default();
    let account = signed_in(&server, &root).await;
    let (swept, pending) = pass(&server, &root, &account).await;

    assert_eq!(
        swept,
        Swept::Nothing,
        "an unreadable recording asked the queue to wait and try again"
    );
    assert_eq!(pending, vec!["not-mine".to_owned()]);
    assert!(
        server.seen().started.is_empty(),
        "an upload was opened for a recording that cannot be read"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A recording the server has taken leaves the queue, and stays out of it.
#[tokio::test]
async fn a_sent_recording_is_not_sent_again() {
    let root = scratch("once");
    let store = Vault::new(&root, key(), DpapiSealer);
    record(&store, "waiting", RecordingState::Uploading);

    let server = Server::default();
    let account = signed_in(&server, &root).await;
    pass(&server, &root, &account).await;
    let after_first = server.seen().finalized.len();

    // A second pass, as the timer makes a minute later.
    let (swept, pending) = pass(&server, &root, &account).await;

    assert_eq!(swept, Swept::Nothing);
    assert!(pending.is_empty());
    assert_eq!(
        server.seen().finalized.len(),
        after_first,
        "the meeting was sent a second time"
    );
    assert_eq!(
        store.reopen("waiting").expect("reopens").manifest().state,
        RecordingState::Uploaded
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A network that is down stalls the pass rather than losing the meeting: the
/// recording stays owed, and the caller is told to wait before trying again.
#[tokio::test]
async fn a_dead_network_leaves_the_meeting_owed() {
    let root = scratch("down");
    let store = Vault::new(&root, key(), DpapiSealer);
    record(&store, "waiting", RecordingState::Uploading);

    let server = Server::default();
    server.seen().network_is_down = true;
    let account = signed_in(&server, &root).await;

    let (swept, pending) = pass(&server, &root, &account).await;

    assert_eq!(swept, Swept::Stalled);
    assert_eq!(pending, vec!["waiting".to_owned()]);
    assert_eq!(
        store.reopen("waiting").expect("reopens").manifest().state,
        RecordingState::Uploading,
        "a failed upload gave the recording away"
    );

    let _ = std::fs::remove_dir_all(&root);
}
