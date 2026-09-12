//! The desktop's side of the NovaBrief API.
//!
//! Only what EF-11 needs for now: sign in, refresh, and read the profile back.
//! The upload path of section 16.4 joins it at lot L3.6.
//!
//! Two rules run through the whole module.
//!
//! **Credentials never appear in an error, a log line or a `Debug`.** The
//! obvious place they leak is not a `println!` - it is a struct that derives
//! `Debug` and ends up formatted inside something else's error. So the types
//! that carry them implement `Debug` by hand.
//!
//! **Nothing is sent anywhere but HTTPS.** A base URL that is plain `http` is
//! refused at construction rather than at request time, because the request
//! that would discover it is the one carrying the password.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use url::Url;

/// How long a request may take before it is abandoned.
///
/// Generous by API standards and deliberately so: the customer is an SME in
/// Douala on a connection that is not always good, and a sign-in that fails
/// after five seconds on a slow morning reads as "this product is broken"
/// rather than "the network is slow".
const TIMEOUT: Duration = Duration::from_secs(30);

/// What went wrong talking to the API.
#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    /// The base URL is not one we are willing to send credentials to.
    #[error("{0}")]
    Configuration(String),

    /// The email or the password is wrong.
    ///
    /// Separated from the rest because it is the only one the person can do
    /// anything about, and the only one that must not be retried automatically.
    #[error("the email address or password is not correct")]
    BadCredentials,

    /// The session is over: the refresh token was rejected.
    ///
    /// Distinct from [`ApiError::BadCredentials`] because the answer is
    /// different - this one means "sign in again", not "you typed it wrong" -
    /// and because a reused refresh token revokes the whole family, so it can
    /// also mean somebody else is holding a copy.
    #[error("this session is no longer valid; sign in again")]
    SessionExpired,

    /// The request never arrived, or the answer never came back.
    #[error("the NovaBrief service could not be reached")]
    Unreachable,

    /// The server answered, unhappily.
    #[error("the NovaBrief service answered {status}")]
    Server {
        /// The HTTP status.
        status: u16,
        /// The stable error code from the Problem Details body, when there was
        /// one. Never the detail text: that can quote what was submitted.
        code: Option<String>,
    },

    /// The answer was not the shape the contract promises.
    #[error("the NovaBrief service answered something unexpected")]
    Protocol,
}

/// A signed-in session.
///
/// `Debug` is written by hand: these are bearer credentials, and the way they
/// escape is a struct that derives `Debug` and gets formatted inside somebody
/// else's error.
#[derive(Clone, Deserialize)]
pub struct TokenPair {
    /// Short-lived; sent as `Authorization: Bearer`.
    pub access_token: String,
    /// Single-use. Presenting it twice revokes the whole family.
    pub refresh_token: String,
    /// Seconds the access token is good for.
    pub expires_in: u64,
}

impl std::fmt::Debug for TokenPair {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TokenPair")
            .field("access_token", &"redacted")
            .field("refresh_token", &"redacted")
            .field("expires_in", &self.expires_in)
            .finish()
    }
}

/// Who is signed in.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Profile {
    /// Display name.
    pub full_name: String,
    /// The address the account was created with, if it has one.
    pub email: Option<String>,
    /// Owner, Admin or Member.
    pub role: String,
    /// Preferred interface language.
    pub locale: String,
    /// Whether the address has been confirmed.
    pub email_verified: bool,
}

#[derive(Serialize)]
struct SignInBody<'a> {
    email: &'a str,
    password: &'a str,
}

impl std::fmt::Debug for SignInBody<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SignInBody(redacted)")
    }
}

#[derive(Serialize)]
struct RefreshBody<'a> {
    refresh_token: &'a str,
}

/// The NovaBrief API, as the desktop sees it.
#[derive(Debug, Clone)]
pub struct ApiClient {
    base: Url,
    http: reqwest::Client,
}

impl ApiClient {
    /// Point the client at `base_url`, usually `https://api.novabrief.cloud`.
    ///
    /// # Errors
    ///
    /// [`ApiError::Configuration`] if the URL cannot be parsed, or is plain
    /// HTTP to anywhere but a loopback address. Refused here rather than at
    /// request time because the request that would find out is the one carrying
    /// somebody's password.
    pub fn new(base_url: &str) -> Result<Self, ApiError> {
        let base = Url::parse(base_url)
            .map_err(|error| ApiError::Configuration(format!("{base_url}: {error}")))?;

        let loopback = matches!(base.host_str(), Some("localhost" | "127.0.0.1" | "::1"));
        if base.scheme() != "https" && !loopback {
            return Err(ApiError::Configuration(format!(
                "{base_url} is not HTTPS; credentials will not be sent over it"
            )));
        }

        let http = reqwest::Client::builder()
            .timeout(TIMEOUT)
            .user_agent(concat!("NovaBrief/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|_| ApiError::Configuration("the HTTP client could not be built".into()))?;

        Ok(Self { base, http })
    }

    /// Exchange an email and password for a session (EF-11).
    ///
    /// # Errors
    ///
    /// [`ApiError::BadCredentials`] when they are wrong, and the variants of
    /// [`ApiError`] for everything else.
    pub async fn sign_in(&self, email: &str, password: &str) -> Result<TokenPair, ApiError> {
        let response = self
            .post("auth/token", &SignInBody { email, password })
            .await?;
        match response.status().as_u16() {
            200 | 201 => decode(response).await,
            401 => Err(ApiError::BadCredentials),
            _ => Err(problem(response).await),
        }
    }

    /// Trade the refresh token for a new pair.
    ///
    /// The caller must store the new refresh token before using the session:
    /// the one passed in is dead the moment this returns.
    ///
    /// # Errors
    ///
    /// [`ApiError::SessionExpired`] when the token is rejected.
    pub async fn refresh(&self, refresh_token: &str) -> Result<TokenPair, ApiError> {
        let response = self
            .post("auth/refresh", &RefreshBody { refresh_token })
            .await?;
        match response.status().as_u16() {
            200 | 201 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    /// Who the access token belongs to.
    ///
    /// # Errors
    ///
    /// [`ApiError::SessionExpired`] when the token is no longer accepted.
    pub async fn profile(&self, access_token: &str) -> Result<Profile, ApiError> {
        let url = self.endpoint("me")?;
        let response = self
            .http
            .get(url)
            .bearer_auth(access_token)
            .send()
            .await
            .map_err(|_| ApiError::Unreachable)?;

        match response.status().as_u16() {
            200 => decode(response).await,
            401 | 403 => Err(ApiError::SessionExpired),
            _ => Err(problem(response).await),
        }
    }

    fn endpoint(&self, path: &str) -> Result<Url, ApiError> {
        self.base
            .join(&format!("/api/v1/{path}"))
            .map_err(|error| ApiError::Configuration(error.to_string()))
    }

    async fn post<B: Serialize>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<reqwest::Response, ApiError> {
        let url = self.endpoint(path)?;
        self.http
            .post(url)
            .json(body)
            .send()
            .await
            // Never the underlying message: it quotes the URL, and on a
            // redirect or a proxy error it can quote the request.
            .map_err(|_| ApiError::Unreachable)
    }
}

async fn decode<T: serde::de::DeserializeOwned>(
    response: reqwest::Response,
) -> Result<T, ApiError> {
    response.json::<T>().await.map_err(|_| ApiError::Protocol)
}

/// Turn an unhappy answer into an error, keeping only the stable code.
///
/// RFC 9457 gives a `detail` as well, and it is deliberately dropped: the API's
/// own validation errors quote the field that failed, and a client that logged
/// them would eventually log a password that was too short.
async fn problem(response: reqwest::Response) -> ApiError {
    let status = response.status().as_u16();
    let code = response
        .json::<serde_json::Value>()
        .await
        .ok()
        .and_then(|body| {
            body.get("code")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        });
    ApiError::Server { status, code }
}

#[cfg(test)]
mod tests {
    use super::{ApiClient, ApiError, TokenPair};

    #[test]
    fn a_plain_http_base_url_is_refused() {
        let error = ApiClient::new("http://api.novabrief.cloud").expect_err("must refuse");
        assert!(matches!(error, ApiError::Configuration(_)));
    }

    /// Refused at construction, not at request time: the request that would
    /// have discovered it is the one carrying the password.
    #[test]
    fn https_is_accepted() {
        assert!(ApiClient::new("https://api.novabrief.cloud").is_ok());
    }

    /// A developer running the API on their own machine has no certificate,
    /// and nothing leaves the loopback interface.
    #[test]
    fn plain_http_to_localhost_is_allowed() {
        assert!(ApiClient::new("http://localhost:8000").is_ok());
        assert!(ApiClient::new("http://127.0.0.1:8000").is_ok());
    }

    #[test]
    fn a_url_that_is_not_a_url_is_refused() {
        assert!(ApiClient::new("not a url").is_err());
    }

    #[test]
    fn the_endpoint_sits_under_the_versioned_prefix() {
        let client = ApiClient::new("https://api.novabrief.cloud").expect("builds");
        assert_eq!(
            client.endpoint("auth/token").expect("joins").as_str(),
            "https://api.novabrief.cloud/api/v1/auth/token"
        );
    }

    /// The way a credential escapes is not a `println!` - it is a struct that
    /// derives `Debug` and gets formatted inside somebody else's error.
    #[test]
    fn tokens_do_not_print_themselves() {
        let pair = TokenPair {
            access_token: "at-secret".to_owned(),
            refresh_token: "rt-secret".to_owned(),
            expires_in: 900,
        };
        let rendered = format!("{pair:?}");

        assert!(!rendered.contains("at-secret"), "{rendered}");
        assert!(!rendered.contains("rt-secret"), "{rendered}");
        assert!(
            rendered.contains("900"),
            "the harmless part is still useful"
        );
    }

    #[test]
    fn a_sign_in_body_does_not_print_itself() {
        let body = super::SignInBody {
            email: "owner@example.cm",
            password: "a very good password",
        };
        assert_eq!(format!("{body:?}"), "SignInBody(redacted)");
    }

    /// Wrong password and dead session need different answers: one is "you
    /// typed it wrong", the other is "sign in again", and only the second one
    /// should ever clear the stored token.
    #[test]
    fn bad_credentials_and_an_expired_session_are_different_errors() {
        assert_ne!(
            ApiError::BadCredentials.to_string(),
            ApiError::SessionExpired.to_string()
        );
    }

    /// Against the deployed API, so the contract is checked rather than
    /// assumed. Ignored by default: CI has no business depending on a service
    /// being up, and a red build that means "the internet is slow" teaches
    /// people to ignore red builds.
    ///
    ///     cargo test -p api-client -- --ignored --nocapture
    #[tokio::test]
    #[ignore = "talks to the deployed API"]
    async fn a_rejected_token_comes_back_as_an_expired_session() {
        let client = ApiClient::new("https://api.novabrief.cloud").expect("builds");

        // Read-only and creates nothing: an invented refresh token can only be
        // refused. A real one would be consumed, which is why this test must
        // never be handed one.
        let error = client
            .refresh("rt-this-token-was-never-issued")
            .await
            .expect_err("an invented token cannot work");

        assert!(
            matches!(error, ApiError::SessionExpired),
            "expected a refused session, got {error:?}"
        );
    }

    /// The message a person sees must not be an HTTP status or a URL.
    #[test]
    fn the_errors_read_as_something_a_person_can_act_on() {
        assert_eq!(
            ApiError::BadCredentials.to_string(),
            "the email address or password is not correct"
        );
        assert_eq!(
            ApiError::Unreachable.to_string(),
            "the NovaBrief service could not be reached"
        );
    }
}
