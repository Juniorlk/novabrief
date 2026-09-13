//! T-01, the network-crash trial of section 24.
//!
//! "Cut the network mid-upload, restore it, and find the meeting complete with
//! no duplicate." Every test here is that sentence taken apart, and the reason
//! [`Transport`] exists at all: with a real HTTP client in the way, "the third
//! part failed and the first two were never resent" could only be checked by
//! unplugging a cable and hoping.
//!
//! The recording is real - a vault, encrypted segments, DPAPI - because the
//! thing being tested is that what arrives at the store is what was recorded.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use api_client::{ApiError, Meeting, UploadSlot, UploadTicket, UploadedPart};
use uploader::{Job, Sent, Transport, UploadError, Uploader};
use vault::{AccountKey, DpapiSealer, Recording, RecordingState, Vault};

const PART_SIZE: u64 = 5 * 1024 * 1024;
const SEGMENT_BYTES: usize = 2 * 1024 * 1024;
const SEGMENTS: u32 = 8;

fn scratch(name: &str) -> PathBuf {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let unique = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("nb-up-{name}-{}-{unique}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

fn account() -> AccountKey {
    AccountKey::derive(b"a-device-secret")
}

/// Recognisable bytes: segment `n` is filled with the byte `n`.
///
/// Not audio. What is being checked is that every byte of the recording
/// arrives once, in order, and a pattern says which segment a stray byte came
/// from when it does not.
fn segment(index: u32) -> Vec<u8> {
    vec![u8::try_from(index).unwrap_or(0xFF); SEGMENT_BYTES]
}

/// A vault holding a recording of 16 MB, and what it should reassemble into.
fn recorded(root: &Path) -> (Vault<DpapiSealer>, Vec<u8>) {
    let store = Vault::new(root, account(), DpapiSealer);
    let mut recording = store
        .begin("mtg-local", "DBG-LOCAL", "mic", "speakers")
        .expect("the vault opens");

    let mut expected = Vec::new();
    for index in 0..SEGMENTS {
        let bytes = segment(index);
        recording
            .append(index, &bytes, 5_000)
            .expect("the segment is stored");
        expected.extend_from_slice(&bytes);
    }
    (store, expected)
}

#[derive(Debug, Default)]
struct Calls {
    declared: u32,
    started: u32,
    resumed: u32,
    /// Part number and bytes, in the order the store received them.
    written: Vec<(u32, Vec<u8>)>,
    finalized: Option<Vec<UploadedPart>>,
    /// What was declared at `start_upload`.
    declared_size: u64,
    declared_sha256: String,
    declared_duration: u64,
    declared_paused: u64,
    /// Parts from this number on are refused, as a dead network would.
    network_dies_at: Option<u32>,
}

/// The server and the store, as far as the uploader can tell.
#[derive(Debug, Default)]
struct Fake {
    calls: Mutex<Calls>,
}

impl Fake {
    fn calls(&self) -> std::sync::MutexGuard<'_, Calls> {
        self.calls.lock().expect("the double is not poisoned")
    }

    fn cut_the_network_at(&self, part_number: u32) {
        self.calls().network_dies_at = Some(part_number);
    }

    fn restore_the_network(&self) {
        self.calls().network_dies_at = None;
    }

    /// Everything the store holds, in part order.
    fn assembled(&self) -> Vec<u8> {
        let calls = self.calls();
        let mut parts: Vec<(u32, Vec<u8>)> = calls.written.clone();
        parts.sort_by_key(|(number, _)| *number);
        parts.into_iter().flat_map(|(_, bytes)| bytes).collect()
    }

    fn ticket(&self, size_bytes: u64) -> UploadTicket {
        let count = u32::try_from(size_bytes.div_ceil(PART_SIZE)).unwrap_or(1);
        UploadTicket {
            upload_id: "upload-1".to_owned(),
            part_size_bytes: PART_SIZE,
            parts: (1..=count)
                .map(|number| UploadSlot {
                    part_number: number,
                    url: format!("https://store.invalid/audio.ogg?part={number}"),
                })
                .collect(),
            expires_in_seconds: 900,
        }
    }
}

fn meeting(id: &str, status: &str) -> Meeting {
    serde_json::from_value(serde_json::json!({
        "id": id,
        "debug_id": "DBG-MTG-7",
        "status": status,
        "title": null,
        "failed_reason": null,
        "duration_seconds": 40,
    }))
    .expect("the double answers the contract")
}

impl Transport for Fake {
    fn declare_meeting(
        &self,
        _access_token: &str,
        _started_at: &str,
        _title: Option<&str>,
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        self.calls().declared += 1;
        async { Ok(meeting("mtg-server", "CREATED")) }
    }

    fn start_upload(
        &self,
        _access_token: &str,
        _meeting_id: &str,
        size_bytes: u64,
        sha256: &str,
        duration_seconds: u64,
        paused_seconds: u64,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        let ticket = {
            let mut calls = self.calls();
            calls.started += 1;
            calls.declared_size = size_bytes;
            calls.declared_sha256 = sha256.to_owned();
            calls.declared_duration = duration_seconds;
            calls.declared_paused = paused_seconds;
            drop(calls);
            self.ticket(size_bytes)
        };
        async move { Ok(ticket) }
    }

    fn resume_upload(
        &self,
        _access_token: &str,
        _meeting_id: &str,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        let ticket = {
            let mut calls = self.calls();
            calls.resumed += 1;
            let size = calls.declared_size;
            drop(calls);
            self.ticket(size)
        };
        async move { Ok(ticket) }
    }

    fn put_part(
        &self,
        url: &str,
        bytes: Vec<u8>,
    ) -> impl std::future::Future<Output = Result<String, ApiError>> + Send {
        let number: u32 = url
            .rsplit_once("part=")
            .map(|(_, n)| n.parse().expect("the double signs its own URLs"))
            .expect("a slot URL");

        let answer = {
            let mut calls = self.calls();
            if calls.network_dies_at.is_some_and(|dead| number >= dead) {
                Err(ApiError::Unreachable)
            } else {
                calls.written.push((number, bytes));
                Ok(format!("etag-{number}"))
            }
        };
        async move { answer }
    }

    fn finalize(
        &self,
        _access_token: &str,
        meeting_id: &str,
        _upload_id: &str,
        parts: &[UploadedPart],
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        self.calls().finalized = Some(parts.to_vec());
        let id = meeting_id.to_owned();
        async move { Ok(meeting(&id, "QUEUED")) }
    }
}

fn job() -> Job {
    Job {
        meeting_id: None,
        debug_id: None,
        started_at: "2026-09-13T10:00:00Z".to_owned(),
        title: None,
        recorded_ms: 40_000,
        paused_ms: 10_000,
    }
}

/// Run one attempt and report what it did, ignoring the progress callback.
async fn attempt(
    uploader: &Uploader<&Fake>,
    recording: &mut Recording,
) -> Result<Meeting, UploadError> {
    let mut ignored = |_: Sent| {};
    uploader
        .attempt("an-access-token", recording, &job(), &mut ignored)
        .await
}

#[tokio::test]
async fn a_recording_reaches_the_server_whole() {
    let root = scratch("whole");
    let (store, expected) = recorded(&root);
    let mut recording = store.reopen("mtg-local").expect("reopens");
    let fake = Fake::default();
    let uploader = Uploader::new(&fake);

    let meeting = attempt(&uploader, &mut recording).await.expect("uploads");

    assert_eq!(meeting.status, "QUEUED");
    assert_eq!(
        fake.assembled(),
        expected,
        "what reached the store is not what was recorded"
    );

    let calls = fake.calls();
    assert_eq!(calls.declared_size, expected.len() as u64);
    // EF-33: what is declared is audio kept, and the pause is declared apart.
    assert_eq!(calls.declared_duration, 40);
    assert_eq!(calls.declared_paused, 10);
    assert_eq!(
        calls.finalized.as_ref().map(Vec::len),
        Some(calls.written.len())
    );
    drop(calls);

    // And the recording knows it no longer owes an upload.
    let after = store.reopen("mtg-local").expect("reopens");
    assert_eq!(after.manifest().state, RecordingState::Uploaded);

    let _ = std::fs::remove_dir_all(&root);
}

/// T-01. The network dies part way through, comes back, and the meeting
/// arrives complete with nothing sent twice.
#[tokio::test]
async fn an_interrupted_upload_resumes_without_sending_anything_twice() {
    let root = scratch("t01");
    let (store, expected) = recorded(&root);
    let mut recording = store.reopen("mtg-local").expect("reopens");
    let fake = Fake::default();
    let uploader = Uploader::new(&fake);

    fake.cut_the_network_at(3);
    let stopped = attempt(&uploader, &mut recording)
        .await
        .expect_err("the network is down");
    assert!(stopped.is_transient(), "{stopped}");
    let sent_before = fake.calls().written.len();
    assert!(sent_before > 0, "nothing got through before the outage");

    fake.restore_the_network();
    attempt(&uploader, &mut recording)
        .await
        .expect("the second attempt finishes");

    let calls = fake.calls();
    let mut numbers: Vec<u32> = calls.written.iter().map(|(number, _)| *number).collect();
    numbers.sort_unstable();
    let mut unique = numbers.clone();
    unique.dedup();
    assert_eq!(numbers, unique, "a part was uploaded twice: {numbers:?}");
    drop(calls);

    assert_eq!(
        fake.assembled(),
        expected,
        "the reassembled upload is not the recording"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// The resume point is on the disk, not in memory.
///
/// The same trial as above, except that everything in memory is thrown away
/// between the two attempts - a new uploader, a recording reopened from the
/// vault - which is what a laptop closed on the way home actually does.
#[tokio::test]
async fn the_resume_point_survives_the_process() {
    let root = scratch("restart");
    let (store, expected) = recorded(&root);
    let fake = Fake::default();

    {
        let mut recording = store.reopen("mtg-local").expect("reopens");
        let uploader = Uploader::new(&fake);
        fake.cut_the_network_at(2);
        attempt(&uploader, &mut recording)
            .await
            .expect_err("the network is down");
    }

    assert!(
        uploader::Progress::path_in(&store.directory_of("mtg-local")).is_file(),
        "nothing was written down, so a restart would send the meeting again"
    );
    let sent_before = fake.calls().written.len();

    {
        // A new process, as far as anything in memory is concerned.
        let mut recording = store.reopen("mtg-local").expect("reopens");
        let uploader = Uploader::new(&fake);
        fake.restore_the_network();
        attempt(&uploader, &mut recording)
            .await
            .expect("the restarted upload finishes");
    }

    let calls = fake.calls();
    let resent = calls.written.len() - sent_before;
    assert!(
        resent < calls.written.len(),
        "the restart resent everything: {resent} of {} parts",
        calls.written.len()
    );
    drop(calls);
    assert_eq!(fake.assembled(), expected);

    // And the record is cleaned up once the server has the meeting.
    assert!(!uploader::Progress::path_in(&store.directory_of("mtg-local")).is_file());

    let _ = std::fs::remove_dir_all(&root);
}

/// ADR-05 lets a meeting be recorded with no network at all, so the
/// declaration of EF-40 happens at the first attempt that has one. It must
/// happen once: a second declaration would create a second meeting for the
/// same audio, and the first would sit in CREATED for ever.
#[tokio::test]
async fn an_offline_recording_is_declared_exactly_once() {
    let root = scratch("declare");
    let (store, _) = recorded(&root);
    let mut recording = store.reopen("mtg-local").expect("reopens");
    let fake = Fake::default();
    let uploader = Uploader::new(&fake);

    fake.cut_the_network_at(1);
    attempt(&uploader, &mut recording)
        .await
        .expect_err("the network is down");
    assert_eq!(fake.calls().declared, 1);

    fake.restore_the_network();
    attempt(&uploader, &mut recording).await.expect("finishes");

    assert_eq!(
        fake.calls().declared,
        1,
        "the meeting was declared a second time"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A resumed upload asks for new signatures. It must not open a second upload:
/// the parts already accepted would then belong to nothing, the customer would
/// pay to send the meeting again, and the abandoned parts would be billed by
/// the store until somebody named them.
#[tokio::test]
async fn resuming_signs_again_rather_than_starting_again() {
    let root = scratch("resign");
    let (store, _) = recorded(&root);
    let mut recording = store.reopen("mtg-local").expect("reopens");
    let fake = Fake::default();
    let uploader = Uploader::new(&fake);

    fake.cut_the_network_at(2);
    attempt(&uploader, &mut recording)
        .await
        .expect_err("the network is down");
    fake.restore_the_network();
    attempt(&uploader, &mut recording).await.expect("finishes");

    let calls = fake.calls();
    assert_eq!(calls.started, 1, "a second upload was opened");
    assert_eq!(calls.resumed, 1, "the second attempt did not re-sign");

    let _ = std::fs::remove_dir_all(&root);
}

/// The progress callback is what the widget draws, and a widget that reports
/// progress the store never confirmed is worse than one that reports none.
#[tokio::test]
async fn progress_counts_only_what_the_store_accepted() {
    let root = scratch("progress");
    let (store, _) = recorded(&root);
    let mut recording = store.reopen("mtg-local").expect("reopens");
    let fake = Fake::default();
    let uploader = Uploader::new(&fake);

    let mut seen: Vec<Sent> = Vec::new();
    let mut record = |sent: Sent| seen.push(sent);
    uploader
        .attempt("token", &mut recording, &job(), &mut record)
        .await
        .expect("uploads");

    assert!(!seen.is_empty());
    let last = seen.last().copied().expect("a reading");
    assert_eq!(last.parts_done, last.parts_total);
    assert!(
        seen.windows(2)
            .all(|pair| pair[1].parts_done >= pair[0].parts_done),
        "progress went backwards: {seen:?}"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A recording that cannot be decrypted is not a network problem, and retrying
/// it every five minutes for ever would hide a real fault behind an upload
/// that never finishes and never complains.
#[test]
fn only_the_faults_that_pass_are_worth_retrying() {
    assert!(UploadError::Api(ApiError::Unreachable).is_transient());
    assert!(UploadError::Api(ApiError::Server {
        status: 503,
        code: None
    })
    .is_transient());
    assert!(!UploadError::Api(ApiError::SessionExpired).is_transient());
    assert!(!UploadError::Vault(vault::VaultError::Decryption).is_transient());
    assert!(!UploadError::Empty.is_transient());
}
