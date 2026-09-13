//! Declaring a meeting, uploading its audio, and reading the report back.
//!
//! Section 16.4. The audio never passes through the API: `finalize-local`
//! hands back presigned slots and the client writes straight into the bucket,
//! which is what keeps a small server from becoming a file proxy for hundreds
//! of megabytes.
//!
//! Every type here is the client's own, deserialised from the contract rather
//! than shared with it. A struct generated from the server's schema would make
//! a field the desktop does not understand into a parse failure, and a desktop
//! that cannot read a meeting is a desktop that cannot upload one.

use serde::{Deserialize, Serialize};

use crate::{decode, problem, ApiClient, ApiError};

/// A meeting the server now knows about (EF-40).
#[derive(Debug, Clone, Deserialize)]
pub struct Meeting {
    /// The server's identifier, which the upload is addressed to.
    pub id: String,
    /// ADR-07, end to end from here to the provider logs.
    pub debug_id: String,
    /// Where the meeting is in the pipeline.
    pub status: String,
    /// What the user called it, if anything.
    pub title: Option<String>,
    /// Why it failed, as a stable code; never a provider message.
    pub failed_reason: Option<String>,
    /// Audio kept, in seconds.
    #[serde(default)]
    pub duration_seconds: i64,
}

/// One presigned slot to write a part of the recording into.
#[derive(Clone, Deserialize)]
pub struct UploadSlot {
    /// One-based, and the order the store reassembles them in.
    pub part_number: u32,
    /// A bearer credential: anyone holding it can write this part.
    pub url: String,
}

impl std::fmt::Debug for UploadSlot {
    /// Never prints the URL.
    ///
    /// A presigned URL is a credential with no token of ours attached, and the
    /// way it escapes is a struct that derives `Debug` and is formatted inside
    /// somebody else's error. The server takes the same care in its own logs.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("UploadSlot")
            .field("part_number", &self.part_number)
            .field("url", &"redacted")
            .finish()
    }
}

/// Where to put the recording, and in what sized pieces.
#[derive(Debug, Clone, Deserialize)]
pub struct UploadTicket {
    /// Identifies the multipart upload at the store.
    pub upload_id: String,
    /// Bytes per part; the last one may be shorter.
    pub part_size_bytes: u64,
    /// One slot per part, in order.
    pub parts: Vec<UploadSlot>,
    /// How long the slots stay signed.
    pub expires_in_seconds: u64,
}

/// One part the store has accepted.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UploadedPart {
    /// Which part.
    pub part_number: u32,
    /// What the store answered, which is how it recognises the part later.
    pub etag: String,
}

/// One line of the transcript.
#[derive(Debug, Clone, Deserialize)]
pub struct TranscriptSegment {
    /// Who spoke, as the provider labelled them.
    pub speaker_tag: String,
    /// The name, if somebody attached one.
    pub speaker_name: Option<String>,
    /// Milliseconds from the start of the recording.
    pub start_ms: i64,
    /// What was said.
    pub text: String,
}

/// A decision the analysis found. Never one it invented.
#[derive(Debug, Clone, Deserialize)]
pub struct Decision {
    /// The decision, as stated.
    pub content: String,
    /// Where in the recording it was said.
    pub source_start_ms: i64,
}

/// A task, with an owner only if somebody was named.
#[derive(Debug, Clone, Deserialize)]
pub struct Task {
    /// What is to be done.
    pub action: String,
    /// Who was named. `None` when nobody was: the model must not guess.
    pub assignee_name: Option<String>,
    /// The deadline as it was said, before any interpretation.
    pub deadline_text: Option<String>,
    /// Where in the recording it was said.
    pub source_start_ms: i64,
}

/// The report itself.
#[derive(Debug, Clone, Deserialize)]
pub struct Report {
    /// A title the analysis proposed.
    pub title: String,
    /// Who took part, as far as the transcript says.
    pub participants: Vec<String>,
    /// The summary, one entry per point.
    pub summary: Vec<String>,
    /// Decisions, with their timestamps.
    pub decisions: Vec<Decision>,
    /// Tasks, with their timestamps.
    pub tasks: Vec<Task>,
}

/// A meeting and everything the server has made of it.
#[derive(Debug, Clone, Deserialize)]
pub struct MeetingDetail {
    /// The meeting itself.
    pub meeting: Meeting,
    /// `None` until the analysis has run.
    pub report: Option<Report>,
    /// The timestamped transcript.
    #[serde(default)]
    pub segments: Vec<TranscriptSegment>,
}

#[derive(Serialize)]
struct DeclareBody<'a> {
    started_at: &'a str,
    title: Option<&'a str>,
    is_private: bool,
}

#[derive(Serialize)]
struct StartUploadBody<'a> {
    size_bytes: u64,
    sha256: &'a str,
    duration_seconds: u64,
    paused_seconds: u64,
}

#[derive(Serialize)]
struct FinalizeBody<'a> {
    upload_id: &'a str,
    parts: &'a [UploadedPart],
    client_version: &'a str,
}

impl ApiClient {
    /// EF-40: say a recording has begun, and get the `debug_id` back.
    ///
    /// Called when recording starts rather than when it ends, so a laptop that
    /// dies mid-meeting still leaves a row to reconcile against.
    ///
    /// # Errors
    ///
    /// [`ApiError::SessionExpired`] when the token is refused, and the other
    /// variants for everything else.
    pub async fn declare_meeting(
        &self,
        access_token: &str,
        started_at: &str,
        title: Option<&str>,
    ) -> Result<Meeting, ApiError> {
        let body = DeclareBody {
            started_at,
            title,
            // EF-22 is V1.1 and the desktop does not offer it, so it does not
            // pretend to decide it either.
            is_private: false,
        };
        let response = self
            .authorised_post("meetings", access_token, &body)
            .await?;
        match response.status().as_u16() {
            200 | 201 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    /// Declare what is about to be uploaded, and get somewhere to put it.
    ///
    /// The size and the digest are sent before a byte moves: that is what
    /// makes the finalisation checkable at all, because the server compares
    /// what the store ends up holding against what was promised here.
    ///
    /// # Errors
    ///
    /// See [`ApiClient::declare_meeting`].
    pub async fn start_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
        size_bytes: u64,
        sha256: &str,
        duration_seconds: u64,
        paused_seconds: u64,
    ) -> Result<UploadTicket, ApiError> {
        let body = StartUploadBody {
            size_bytes,
            sha256,
            duration_seconds,
            paused_seconds,
        };
        let response = self
            .authorised_post(
                &format!("meetings/{meeting_id}/finalize-local"),
                access_token,
                &body,
            )
            .await?;
        match response.status().as_u16() {
            200 | 201 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    /// Sign the slots again for an upload already under way (EF-18).
    ///
    /// The signatures last fifteen minutes and an outage often does not, so
    /// this is what a client comes back to. Nothing about the upload changes:
    /// the parts already accepted keep their numbers, and only what is missing
    /// is sent.
    ///
    /// # Errors
    ///
    /// See [`ApiClient::declare_meeting`].
    pub async fn resume_upload(
        &self,
        access_token: &str,
        meeting_id: &str,
    ) -> Result<UploadTicket, ApiError> {
        let response = self
            .authorised_post(
                &format!("meetings/{meeting_id}/upload-parts"),
                access_token,
                &serde_json::Value::Null,
            )
            .await?;
        match response.status().as_u16() {
            200 | 201 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    /// Write one part into its slot, and take the tag the store answers with.
    ///
    /// This is the only request in the client that does not go to NovaBrief:
    /// the URL is Cloudflare's, signed by our API, and the audio goes there
    /// directly. No bearer token is attached - the signature *is* the
    /// authorisation, and sending the session token to a third party would be
    /// handing it to whoever the presigned URL points at.
    ///
    /// # Errors
    ///
    /// [`ApiError::Unreachable`] if the request does not complete, and
    /// [`ApiError::Server`] if the store refuses it - a slot whose signature
    /// has expired answers 403, which is what makes the caller ask for new
    /// ones rather than give up.
    pub async fn put_part(&self, url: &str, bytes: Vec<u8>) -> Result<String, ApiError> {
        let response = self
            .http
            .put(url)
            .body(bytes)
            .send()
            .await
            .map_err(|_| ApiError::Unreachable)?;

        if !response.status().is_success() {
            return Err(ApiError::Server {
                status: response.status().as_u16(),
                code: None,
            });
        }
        response
            .headers()
            .get(reqwest::header::ETAG)
            .and_then(|value| value.to_str().ok())
            .map(|etag| etag.trim_matches('"').to_owned())
            .ok_or(ApiError::Protocol)
    }

    /// Close the upload and let the server queue the work (EF-40).
    ///
    /// # Errors
    ///
    /// See [`ApiClient::declare_meeting`].
    pub async fn finalize(
        &self,
        access_token: &str,
        meeting_id: &str,
        upload_id: &str,
        parts: &[UploadedPart],
    ) -> Result<Meeting, ApiError> {
        let body = FinalizeBody {
            upload_id,
            parts,
            client_version: env!("CARGO_PKG_VERSION"),
        };
        let response = self
            .authorised_post(
                &format!("meetings/{meeting_id}/finalize"),
                access_token,
                &body,
            )
            .await?;
        match response.status().as_u16() {
            200..=202 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    /// The organization's meetings, newest first.
    ///
    /// # Errors
    ///
    /// See [`ApiClient::declare_meeting`].
    pub async fn meetings(&self, access_token: &str) -> Result<Vec<Meeting>, ApiError> {
        self.authorised_get("meetings", access_token).await
    }

    /// One meeting, with its report and transcript when they exist.
    ///
    /// # Errors
    ///
    /// See [`ApiClient::declare_meeting`].
    pub async fn meeting_detail(
        &self,
        access_token: &str,
        meeting_id: &str,
    ) -> Result<MeetingDetail, ApiError> {
        self.authorised_get(&format!("meetings/{meeting_id}/detail"), access_token)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::{Meeting, MeetingDetail, UploadSlot, UploadTicket};

    /// The contract, as the server actually answers it.
    #[test]
    fn a_declared_meeting_parses() {
        let meeting: Meeting = serde_json::from_str(
            r#"{"id":"01a0-7","debug_id":"DBG-MTG-1","status":"CREATED","title":null,
                "failed_reason":null,"duration_seconds":0,"language":null,
                "started_at":"2026-09-13T10:00:00Z","is_private":false,
                "created_by":"01a0-1","purge_at":null,"created_at":"2026-09-13T10:00:00Z",
                "completed_at":null}"#,
        )
        .expect("the declaration answer parses");
        assert_eq!(meeting.debug_id, "DBG-MTG-1");
    }

    /// Fields the desktop does not know about must not break it. The server
    /// gains columns faster than an installed client is replaced, and a parse
    /// failure here is a meeting that cannot be uploaded at all.
    #[test]
    fn an_unknown_field_does_not_break_the_client() {
        let meeting: Meeting = serde_json::from_str(
            r#"{"id":"1","debug_id":"D","status":"QUEUED","title":null,
                "failed_reason":null,"something_added_next_year":42}"#,
        )
        .expect("unknown fields are ignored");
        assert_eq!(meeting.status, "QUEUED");
    }

    #[test]
    fn a_ticket_parses() {
        let ticket: UploadTicket = serde_json::from_str(
            r#"{"upload_id":"u-1","part_size_bytes":5242880,
                "parts":[{"part_number":1,"url":"https://example.invalid/1"}],
                "expires_in_seconds":900}"#,
        )
        .expect("the ticket parses");
        assert_eq!(ticket.parts[0].part_number, 1);
    }

    /// A presigned URL is a credential. It must not be printable.
    #[test]
    fn a_slot_never_prints_its_url() {
        let slot = UploadSlot {
            part_number: 1,
            url: "https://bucket.example.invalid/part?X-Amz-Signature=secret".to_owned(),
        };
        let printed = format!("{slot:?}");
        assert!(!printed.contains("secret"), "{printed}");
        assert!(printed.contains("redacted"), "{printed}");
    }

    /// A meeting that has not been analysed yet has no report, and that is a
    /// normal answer rather than a missing field.
    #[test]
    fn a_detail_without_a_report_parses() {
        let detail: MeetingDetail = serde_json::from_str(
            r#"{"meeting":{"id":"1","debug_id":"D","status":"TRANSCRIBING",
                "title":null,"failed_reason":null},"report":null,"segments":[]}"#,
        )
        .expect("an unfinished meeting parses");
        assert!(detail.report.is_none());
        assert!(detail.segments.is_empty());
    }
}
