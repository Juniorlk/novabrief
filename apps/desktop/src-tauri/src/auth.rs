//! Who is signed in, and how the session stays alive (EF-11).
//!
//! "Once linked, never sign in again unless revoked" is a promise about the
//! refresh token, and refresh tokens rotate: presenting one returns a new one,
//! and presenting it **twice revokes the whole family**. Everything here is
//! shaped by that single fact.
//!
//! It is why the session sits behind an async mutex rather than a lock taken
//! and dropped around each call. Two requests that both found the access token
//! expired would otherwise both refresh, the second would present a token the
//! first had already spent, and the server would sign the machine out - on a
//! laptop holding meetings that have not been uploaded yet.
//!
//! It is also why the new token is written to the disk **before** the session
//! is updated. The moment the server answers, the copy on disk is dead; a
//! process that used the new access token and then failed to store its
//! companion would be signed out at the next start.

use std::time::{Duration, Instant};

use api_client::{ApiClient, ApiError, Profile, TokenPair};
use serde::Serialize;
use tokio::sync::Mutex;
use vault::{CredentialStore, DpapiSealer};

/// How long before it expires an access token is replaced.
///
/// A request that takes the token, waits behind a slow upload and then arrives
/// at the server after it has expired fails for no good reason. A minute of
/// margin costs one extra refresh a day and removes the whole class.
const RENEW_BEFORE: Duration = Duration::from_secs(60);

/// What the desktop needs from the authentication endpoints.
///
/// A trait for the same reason the uploader has one: rotation is the part that
/// can go wrong, and "the second concurrent caller did not spend a second
/// token" is not something a real server can be asked to demonstrate on
/// demand.
pub trait Identity {
    /// Email and password for a session.
    fn sign_in(
        &self,
        email: &str,
        password: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send;

    /// Trade the refresh token for a new pair. The old one dies here.
    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send;

    /// Who the access token belongs to.
    fn profile(
        &self,
        access_token: &str,
    ) -> impl std::future::Future<Output = Result<Profile, ApiError>> + Send;
}

/// A borrowed identity is an identity.
///
/// One client serves the whole application - it holds the connection pool -
/// so an account can be given a reference rather than demanding ownership.
impl<I: Identity + Sync> Identity for &I {
    fn sign_in(
        &self,
        email: &str,
        password: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        I::sign_in(self, email, password)
    }

    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        I::refresh(self, refresh_token)
    }

    fn profile(
        &self,
        access_token: &str,
    ) -> impl std::future::Future<Output = Result<Profile, ApiError>> + Send {
        I::profile(self, access_token)
    }
}

/// A shared identity is an identity.
///
/// The session and the upload queue hold the same client, so a renewal made
/// for one is seen by the other.
impl<I: Identity + Send + Sync> Identity for std::sync::Arc<I> {
    fn sign_in(
        &self,
        email: &str,
        password: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        I::sign_in(self, email, password)
    }

    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        I::refresh(self, refresh_token)
    }

    fn profile(
        &self,
        access_token: &str,
    ) -> impl std::future::Future<Output = Result<Profile, ApiError>> + Send {
        I::profile(self, access_token)
    }
}

impl Identity for ApiClient {
    fn sign_in(
        &self,
        email: &str,
        password: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        Self::sign_in(self, email, password)
    }

    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        Self::refresh(self, refresh_token)
    }

    fn profile(
        &self,
        access_token: &str,
    ) -> impl std::future::Future<Output = Result<Profile, ApiError>> + Send {
        Self::profile(self, access_token)
    }
}

/// What the user interface draws about the account.
///
/// Carries no token. The front end never needs one - every request goes
/// through a command here - and a token that reached the WebView would be one
/// an extension or a devtools session could read.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Identified {
    /// Display name.
    pub full_name: String,
    /// The address the account was created with.
    pub email: Option<String>,
    /// Owner, Admin or Member.
    pub role: String,
    /// Preferred language, which the interface follows.
    pub locale: String,
    /// Whether the address has been confirmed (EF-02).
    pub email_verified: bool,
}

impl From<Profile> for Identified {
    fn from(profile: Profile) -> Self {
        Self {
            full_name: profile.full_name,
            email: profile.email,
            role: profile.role,
            locale: profile.locale,
            email_verified: profile.email_verified,
        }
    }
}

/// A live session: a token, and when it stops working.
///
/// The profile is deliberately **not** in here. A renewal happens every few
/// minutes and does not change who anybody is, so keeping the two together
/// would either cost a profile request per renewal or force a placeholder
/// profile into existence - and a profile with an empty name is a value the
/// interface would eventually draw.
struct Live {
    access_token: String,
    expires_at: Instant,
}

impl std::fmt::Debug for Live {
    /// Never prints the token.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Live")
            .field("access_token", &"redacted")
            .finish_non_exhaustive()
    }
}

/// The signed-in account, and the only place a token lives.
#[derive(Debug)]
pub struct Account<I: Identity> {
    identity: I,
    credentials: CredentialStore<DpapiSealer>,
    session: Mutex<Option<Live>>,
    /// Who the session belongs to, once the server has been asked.
    profile: Mutex<Option<Profile>>,
}

impl<I: Identity> Account<I> {
    /// An account that talks through `identity` and remembers in `credentials`.
    #[must_use]
    pub fn new(identity: I, credentials: CredentialStore<DpapiSealer>) -> Self {
        Self {
            identity,
            credentials,
            session: Mutex::new(None),
            profile: Mutex::new(None),
        }
    }

    /// Whether this machine has been linked to an account.
    ///
    /// Answered from the disk rather than from the session, so a cold start
    /// can draw the right screen before any request is made.
    #[must_use]
    pub fn is_linked(&self) -> bool {
        self.credentials.refresh_token().ok().flatten().is_some()
    }

    /// Sign in with an email and a password (EF-11).
    ///
    /// # Errors
    ///
    /// A message when they are wrong. A refresh token that arrives but cannot
    /// be stored is also an error: the sign-in would appear to work and the
    /// next start would ask again, which reads as the product forgetting
    /// people.
    pub async fn sign_in(&self, email: &str, password: &str) -> Result<Identified, String> {
        let pair = self
            .identity
            .sign_in(email, password)
            .await
            .map_err(|error| error.to_string())?;

        let token = {
            let mut session = self.session.lock().await;
            self.install(&mut session, pair)?
        };
        self.learn_who(&token).await
    }

    /// Bring back the session this machine was left with.
    ///
    /// # Errors
    ///
    /// A message when the stored token is no longer accepted - revoked, or
    /// presented twice - which means somebody has to sign in again.
    pub async fn restore(&self) -> Result<Identified, String> {
        let stored = self
            .credentials
            .refresh_token()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "this machine is not linked to an account".to_owned())?;

        let token = {
            let mut session = self.session.lock().await;
            let pair = match self.identity.refresh(&stored).await {
                Ok(pair) => pair,
                Err(error) => {
                    // A refused token is dead, and keeping it would make every
                    // later attempt present it again - which is the thing that
                    // revokes a family. Forgotten locally, so the next screen
                    // is a sign-in rather than the same failure for ever.
                    if matches!(error, ApiError::SessionExpired) {
                        let _ = self.credentials.forget();
                    }
                    return Err(error.to_string());
                }
            };
            self.install(&mut session, pair)?
        };
        self.learn_who(&token).await
    }

    /// The token to put on a request, renewed if it is about to expire.
    ///
    /// # Errors
    ///
    /// A message when nobody is signed in, or the session could not be
    /// renewed.
    pub async fn access_token(&self) -> Result<String, String> {
        let mut session = self.session.lock().await;

        if let Some(live) = session.as_ref() {
            if live.expires_at.saturating_duration_since(Instant::now()) > RENEW_BEFORE {
                return Ok(live.access_token.clone());
            }
        }

        // The lock is deliberately still held. Releasing it to renew would let
        // a second caller find the same expired token and spend a second
        // refresh token on it, and the server revokes the family when one is
        // presented twice.
        let stored = self
            .credentials
            .refresh_token()
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "this machine is not linked to an account".to_owned())?;

        let pair = self
            .identity
            .refresh(&stored)
            .await
            .map_err(|error| error.to_string())?;
        self.install(&mut session, pair)
    }

    /// Who is signed in, without asking the server.
    pub async fn identified(&self) -> Option<Identified> {
        self.profile.lock().await.clone().map(Identified::from)
    }

    /// Forget the session on this machine.
    ///
    /// # Errors
    ///
    /// A message if the stored token cannot be removed, which is the one
    /// failure that matters: a machine somebody asked to sign out of must not
    /// keep a credential.
    pub async fn sign_out(&self) -> Result<(), String> {
        self.session.lock().await.take();
        self.profile.lock().await.take();
        self.credentials.forget().map_err(|error| error.to_string())
    }

    /// Ask the server who this is again, for a screen that wants it fresh.
    ///
    /// # Errors
    ///
    /// A message when the session is no longer accepted.
    pub async fn refresh_profile(&self) -> Result<Identified, String> {
        let token = self.access_token().await?;
        self.learn_who(&token).await
    }

    /// Write the refresh token down, then make the access token current.
    ///
    /// In that order, and never the other way. The stored token is dead the
    /// moment the server answered; a process that used the new access token
    /// and then failed to write its companion would be signed out at the next
    /// start, holding meetings it can no longer upload.
    fn install(&self, session: &mut Option<Live>, pair: TokenPair) -> Result<String, String> {
        self.credentials
            .store(&pair.refresh_token)
            .map_err(|error| error.to_string())?;

        let access_token = pair.access_token;
        *session = Some(Live {
            access_token: access_token.clone(),
            expires_at: Instant::now() + Duration::from_secs(pair.expires_in),
        });
        Ok(access_token)
    }

    /// Ask the server who this is, and remember the answer.
    ///
    /// Costs a request, so it does not happen on every renewal: renewals come
    /// every few minutes and a display name does not change that often.
    async fn learn_who(&self, access_token: &str) -> Result<Identified, String> {
        let profile = self
            .identity
            .profile(access_token)
            .await
            .map_err(|error| error.to_string())?;
        *self.profile.lock().await = Some(profile.clone());
        Ok(Identified::from(profile))
    }
}
