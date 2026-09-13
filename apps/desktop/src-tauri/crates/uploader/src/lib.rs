//! Getting a recording off the laptop and onto the server (EF-18, section 16.4).
//!
//! ADR-05 makes the network optional during the meeting, which moves the whole
//! difficulty here: by the time anything is uploaded the audio already exists,
//! and losing it is no longer acceptable at any point. So the rule this crate
//! is built around is that **an upload may be interrupted anywhere and resumed
//! from the disk**, including after the process is gone.
//!
//! One attempt at a time, and no timer inside. [`Uploader::attempt`] does as
//! much as the network allows and reports what stopped it; the caller decides
//! when to try again with a [`Backoff`]. That keeps "retry forever, backing off
//! to five minutes" in one visible place, and it is what lets the whole of
//! EF-18 - including losing the network halfway and coming back to it - be
//! tested without sleeping and without a server.

pub mod assembly;
pub mod backoff;
pub mod progress;

use std::path::PathBuf;

use api_client::{ApiError, Meeting, UploadTicket, UploadedPart};
use vault::{Recording, RecordingState, VaultError};

pub use assembly::{measure, Measurement, Parts};
pub use backoff::Backoff;
pub use progress::Progress;

/// What stopped an upload.
#[derive(Debug, thiserror::Error)]
pub enum UploadError {
    /// The server or the store refused, or could not be reached.
    #[error("{0}")]
    Api(#[from] ApiError),

    /// A segment could not be read back.
    #[error("{0}")]
    Vault(#[from] VaultError),

    /// The record of what has been uploaded could not be kept.
    #[error("{path}: {source}")]
    Io {
        /// The file that could not be written.
        path: PathBuf,
        /// Why.
        #[source]
        source: std::io::Error,
    },

    /// The recording is shorter than the ticket expects.
    ///
    /// A contradiction rather than a network problem: the size was declared
    /// from the same recording moments earlier, so this means the vault
    /// changed underneath. Retrying would send a truncated meeting.
    #[error("the recording no longer matches what was declared to the server")]
    Changed,

    /// There is nothing to upload.
    #[error("this recording holds no audio")]
    Empty,
}

impl UploadError {
    /// Whether trying again later could succeed.
    ///
    /// A network that is down comes back; a recording that contradicts itself
    /// does not. Retrying the second forever would hide a real fault behind an
    /// upload that never finishes and never complains.
    #[must_use]
    pub const fn is_transient(&self) -> bool {
        match self {
            Self::Api(error) => matches!(
                error,
                ApiError::Unreachable | ApiError::Server { .. } | ApiError::Protocol
            ),
            // A disk that is full or a file that is locked by an antivirus are
            // both ordinary and both pass.
            Self::Io { .. } => true,
            Self::Vault(_) | Self::Changed | Self::Empty => false,
        }
    }
}

/// What the desktop asks the uploader to send.
#[derive(Debug, Clone)]
pub struct Job {
    /// The meeting the server knows, if it has been declared.
    ///
    /// `None` when the recording was made offline: the declaration of EF-40
    /// happens at the start of a meeting when there is a network, and at the
    /// first upload attempt when there was not.
    pub meeting_id: Option<String>,
    /// The `debug_id` the server gave when it was declared (ADR-07).
    ///
    /// `None` alongside a `None` meeting: neither exists until the
    /// declaration, and the local manifest carries a placeholder until then.
    pub debug_id: Option<String>,
    /// When the recording began, RFC 3339.
    pub started_at: String,
    /// What to call it, if the user said.
    pub title: Option<String>,
    /// Audio kept, in milliseconds. EF-33 says this is what gets billed.
    pub recorded_ms: u64,
    /// Audio dropped by pauses, in milliseconds.
    pub paused_ms: u64,
}

/// How far along an upload is, for the widget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sent {
    /// Parts the store has accepted.
    pub parts_done: u32,
    /// Parts in total.
    pub parts_total: u32,
}

/// Everything the uploader needs from the network.
///
/// A trait rather than the client itself, so that the sequence this crate is
/// responsible for - declare, start, send, confirm, resume - can be tested
/// against a double that loses the network exactly where a test wants it to.
/// With a real HTTP client in the way, "the connection died after the second
/// part and the third was never resent" would be untestable, and it is the one
/// behaviour EF-18 is about.
pub trait Transport {
    /// EF-40: announce a recording and take the `debug_id` back.
    fn declare_meeting(
        &self,
        access_token: &str,
        started_at: &str,
        title: Option<&str>,
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send;

    /// Declare what is about to be sent, and get somewhere to put it.
    fn start_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
        size_bytes: u64,
        sha256: &str,
        duration_seconds: u64,
        paused_seconds: u64,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send;

    /// Sign the slots again for an upload already under way.
    fn resume_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send;

    /// Write one part, and take the tag the store answers with.
    fn put_part(
        &self,
        url: &str,
        bytes: Vec<u8>,
    ) -> impl std::future::Future<Output = Result<String, ApiError>> + Send;

    /// Close the upload and let the server queue the work.
    fn finalize(
        &self,
        access_token: &str,
        meeting_id: &str,
        upload_id: &str,
        parts: &[UploadedPart],
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send;
}

/// A borrowed transport is a transport.
///
/// One client serves the whole application - it holds the connection pool -
/// so the uploader takes a reference rather than demanding ownership of it.
impl<T: Transport + Sync> Transport for &T {
    fn declare_meeting(
        &self,
        access_token: &str,
        started_at: &str,
        title: Option<&str>,
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        T::declare_meeting(self, access_token, started_at, title)
    }

    fn start_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
        size_bytes: u64,
        sha256: &str,
        duration_seconds: u64,
        paused_seconds: u64,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        T::start_upload(
            self,
            access_token,
            meeting_id,
            size_bytes,
            sha256,
            duration_seconds,
            paused_seconds,
        )
    }

    fn resume_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        T::resume_upload(self, access_token, meeting_id)
    }

    fn put_part(
        &self,
        url: &str,
        bytes: Vec<u8>,
    ) -> impl std::future::Future<Output = Result<String, ApiError>> + Send {
        T::put_part(self, url, bytes)
    }

    fn finalize(
        &self,
        access_token: &str,
        meeting_id: &str,
        upload_id: &str,
        parts: &[UploadedPart],
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        T::finalize(self, access_token, meeting_id, upload_id, parts)
    }
}

impl Transport for api_client::ApiClient {
    fn declare_meeting(
        &self,
        access_token: &str,
        started_at: &str,
        title: Option<&str>,
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        Self::declare_meeting(self, access_token, started_at, title)
    }

    fn start_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
        size_bytes: u64,
        sha256: &str,
        duration_seconds: u64,
        paused_seconds: u64,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        Self::start_upload(
            self,
            access_token,
            meeting_id,
            size_bytes,
            sha256,
            duration_seconds,
            paused_seconds,
        )
    }

    fn resume_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
    ) -> impl std::future::Future<Output = Result<UploadTicket, ApiError>> + Send {
        Self::resume_upload(self, access_token, meeting_id)
    }

    fn put_part(
        &self,
        url: &str,
        bytes: Vec<u8>,
    ) -> impl std::future::Future<Output = Result<String, ApiError>> + Send {
        Self::put_part(self, url, bytes)
    }

    fn finalize(
        &self,
        access_token: &str,
        meeting_id: &str,
        upload_id: &str,
        parts: &[UploadedPart],
    ) -> impl std::future::Future<Output = Result<Meeting, ApiError>> + Send {
        Self::finalize(self, access_token, meeting_id, upload_id, parts)
    }
}

/// Sends one recording, once per call.
#[derive(Debug)]
pub struct Uploader<T: Transport> {
    transport: T,
}

impl<T: Transport> Uploader<T> {
    /// An uploader that talks through `transport`.
    #[must_use]
    pub const fn new(transport: T) -> Self {
        Self { transport }
    }

    /// Send as much of the recording as the network allows.
    ///
    /// Returns the meeting once the server has it. Anything else is reported
    /// with whatever stopped the attempt; ask [`UploadError::is_transient`]
    /// whether trying again could help, and nothing that was already accepted
    /// is sent twice when it is.
    ///
    /// # Errors
    ///
    /// [`UploadError`], for every reason an upload can stop.
    pub async fn attempt(
        &self,
        access_token: &str,
        recording: &mut Recording,
        job: &Job,
        report: &mut dyn FnMut(Sent),
    ) -> Result<Meeting, UploadError> {
        let directory = recording.directory().to_path_buf();
        let record = Progress::path_in(&directory);
        let mut progress = Progress::load(&record);

        // Weighed every time rather than trusted from the record: the file on
        // the disk is what will be sent, and a digest carried over from an
        // earlier attempt would describe something that is no longer there.
        let measurement = measure(recording)?;
        if measurement.size_bytes == 0 {
            return Err(UploadError::Empty);
        }

        let (meeting_id, debug_id) = self.identify(access_token, job, progress.as_ref()).await?;

        // A record for a different meeting or a different recording is not a
        // resume point. Starting from it would send this meeting under the
        // part list of another one.
        if progress
            .as_ref()
            .is_some_and(|p| p.meeting_id != meeting_id || p.sha256 != measurement.sha256)
        {
            progress = None;
        }

        let (ticket, mut progress) = match progress {
            Some(existing) => {
                let ticket = self
                    .transport
                    .resume_upload(access_token, &meeting_id)
                    .await?;
                (ticket, existing)
            }
            None => {
                let ticket = self
                    .transport
                    .start_upload(
                        access_token,
                        &meeting_id,
                        measurement.size_bytes,
                        &measurement.sha256,
                        job.recorded_ms / 1000,
                        job.paused_ms / 1000,
                    )
                    .await?;
                let fresh = Progress {
                    meeting_id: meeting_id.clone(),
                    debug_id: debug_id.clone(),
                    upload_id: ticket.upload_id.clone(),
                    size_bytes: measurement.size_bytes,
                    sha256: measurement.sha256.clone(),
                    confirmed: Vec::new(),
                };
                // Written before the first part goes out. A crash between the
                // two would otherwise leave an upload open at the store that
                // nothing on this machine remembers, billed until somebody
                // names it - and nobody ever would.
                fresh.save(&record)?;
                (ticket, fresh)
            }
        };

        // The store answered about a different upload than the one we have
        // parts for, so those parts belong to nothing.
        if ticket.upload_id != progress.upload_id {
            progress.upload_id = ticket.upload_id.clone();
            progress.confirmed.clear();
            progress.save(&record)?;
        }

        let total = ticket.parts.len() as u32;
        let mut parts = Parts::new(recording, ticket.part_size_bytes);
        for slot in &ticket.parts {
            // Read even when the part is already confirmed: the stream is
            // sequential, and skipping the read would send part four's bytes
            // under part three's number.
            let bytes = parts.next_part()?.ok_or(UploadError::Changed)?;

            if !progress.holds(slot.part_number) {
                let etag = self.transport.put_part(&slot.url, bytes).await?;
                progress.accept(UploadedPart {
                    part_number: slot.part_number,
                    etag,
                });
                // After each part, not at the end. The whole point of the
                // record is the attempt that does not reach the end.
                progress.save(&record)?;
            }
            report(Sent {
                parts_done: progress.confirmed.len() as u32,
                parts_total: total,
            });
        }

        let meeting = self
            .transport
            .finalize(
                access_token,
                &meeting_id,
                &progress.upload_id,
                &progress.confirmed,
            )
            .await?;

        // The server has it. The local copy stays until its retention runs out
        // (ADR-06) but it is no longer owed an upload.
        recording.set_state(RecordingState::Uploaded)?;
        Progress::forget(&record);
        Ok(meeting)
    }

    /// The meeting this recording belongs to, declaring it if nobody has.
    ///
    /// The record on the disk wins over the job. A recording made with no
    /// network is declared at the first attempt that has one, and if that
    /// answer were not written down before anything else happened, the next
    /// attempt would declare a second meeting for the same audio - and the
    /// first would sit in CREATED for ever, counted and never delivered.
    async fn identify(
        &self,
        access_token: &str,
        job: &Job,
        progress: Option<&Progress>,
    ) -> Result<(String, String), UploadError> {
        if let Some(known) = progress {
            return Ok((known.meeting_id.clone(), known.debug_id.clone()));
        }
        if let Some(known) = job.meeting_id.clone() {
            return Ok((known, job.debug_id.clone().unwrap_or_default()));
        }
        let declared = self
            .transport
            .declare_meeting(access_token, &job.started_at, job.title.as_deref())
            .await?;
        Ok((declared.id, declared.debug_id))
    }
}
