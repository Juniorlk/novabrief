//! EF-11, and the one thing about it that can go badly wrong.
//!
//! Refresh tokens are single-use: presenting one returns a new one, and
//! presenting it twice revokes the whole family. Every test here is about the
//! orderings that would present one twice - and none of them is a thing a real
//! server can be asked to demonstrate on demand, which is why the account
//! talks to a double.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use api_client::{ApiError, Profile, TokenPair};
use novabrief_desktop_lib::auth::{Account, Identity};
use vault::{CredentialStore, DpapiSealer};

fn scratch(name: &str) -> PathBuf {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let unique = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("nb-auth-{name}-{}-{unique}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("a scratch directory");
    dir
}

#[derive(Debug, Default)]
struct Calls {
    signed_in: u32,
    /// The refresh tokens presented, in order.
    presented: Vec<String>,
    profiles: u32,
    /// Handed out by the next refresh, so a test can watch rotation happen.
    next_refresh: u32,
    /// Seconds the next access token is good for.
    lifetime: u64,
    /// Refresh answers with this instead, as a revoked family would.
    refuse_refresh: bool,
}

/// The authentication endpoints, as far as the account can tell.
#[derive(Debug)]
struct Server {
    calls: Mutex<Calls>,
}

impl Server {
    fn new(lifetime: u64) -> Self {
        Self {
            calls: Mutex::new(Calls {
                lifetime,
                ..Calls::default()
            }),
        }
    }

    fn calls(&self) -> std::sync::MutexGuard<'_, Calls> {
        self.calls.lock().expect("the double is not poisoned")
    }

    fn refuse_refresh(&self) {
        self.calls().refuse_refresh = true;
    }

    fn pair(calls: &mut Calls) -> TokenPair {
        calls.next_refresh += 1;
        TokenPair {
            access_token: format!("access-{}", calls.next_refresh),
            refresh_token: format!("refresh-{}", calls.next_refresh),
            expires_in: calls.lifetime,
        }
    }
}

impl Identity for Server {
    fn sign_in(
        &self,
        _email: &str,
        _password: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        let pair = {
            let mut calls = self.calls();
            calls.signed_in += 1;
            Server::pair(&mut calls)
        };
        async move { Ok(pair) }
    }

    fn refresh(
        &self,
        refresh_token: &str,
    ) -> impl std::future::Future<Output = Result<TokenPair, ApiError>> + Send {
        let presented = refresh_token.to_owned();
        async move {
            // Yields before answering, and that is the point rather than
            // realism. A double that answers without ever suspending is polled
            // to completion before a second caller starts, so a race test
            // against it passes whatever the code under it does - which is
            // exactly what this one did until the yield was added.
            tokio::task::yield_now().await;

            let mut calls = self.calls();
            calls.presented.push(presented);
            if calls.refuse_refresh {
                return Err(ApiError::SessionExpired);
            }
            // A renewal that landed because the previous token had a short
            // life must not hand out another short one, or every test would
            // refresh in a loop.
            calls.lifetime = 3600;
            Ok(Server::pair(&mut calls))
        }
    }

    fn profile(
        &self,
        _access_token: &str,
    ) -> impl std::future::Future<Output = Result<Profile, ApiError>> + Send {
        self.calls().profiles += 1;
        async {
            Ok(Profile {
                full_name: "Awa Ndiaye".to_owned(),
                email: Some("awa@example.invalid".to_owned()),
                role: "Owner".to_owned(),
                locale: "fr".to_owned(),
                email_verified: true,
            })
        }
    }
}

/// An account against a double the test keeps its own handle on.
fn account<'a>(server: &'a Server, root: &Path) -> (Account<&'a Server>, PathBuf) {
    let path = root.join("credentials.bin");
    let store = CredentialStore::new(path.clone(), DpapiSealer);
    (Account::new(server, store), path)
}

/// The stored token, read the way the next start would read it.
fn stored(path: &Path) -> Option<String> {
    CredentialStore::new(path.to_path_buf(), DpapiSealer)
        .refresh_token()
        .expect("the store is readable")
}

#[tokio::test]
async fn signing_in_links_the_machine() {
    let root = scratch("link");
    let server = Server::new(3600);
    let (account, path) = account(&server, &root);

    assert!(!account.is_linked());
    let who = account
        .sign_in("awa@example.invalid", "a-password")
        .await
        .expect("signs in");

    assert_eq!(who.full_name, "Awa Ndiaye");
    assert!(account.is_linked(), "the machine was not linked");
    assert_eq!(stored(&path).as_deref(), Some("refresh-1"));

    let _ = std::fs::remove_dir_all(&root);
}

/// The password is sent once and forgotten. What persists is the refresh
/// token, and EF-11 promises never asking again is a property of that.
#[tokio::test]
async fn the_password_is_never_written_down() {
    let root = scratch("password");
    let server = Server::new(3600);
    let (account, path) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "correct-horse-battery-staple")
        .await
        .expect("signs in");

    let sealed = std::fs::read(&path).expect("the credential file");
    let haystack = String::from_utf8_lossy(&sealed);
    assert!(
        !haystack.contains("correct-horse-battery-staple"),
        "the password reached the disk"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// Rotation, as the server performs it: the token that comes back replaces the
/// one that was presented, and the old one is gone from the disk.
#[tokio::test]
async fn a_restored_session_replaces_the_token_it_used() {
    let root = scratch("rotate");
    let server = Server::new(3600);
    let (account, path) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");
    assert_eq!(stored(&path).as_deref(), Some("refresh-1"));

    account.restore().await.expect("restores");

    assert_eq!(
        stored(&path).as_deref(),
        Some("refresh-2"),
        "the machine kept a token the server has already spent"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// The one that matters.
///
/// Two callers find the access token expired at the same moment. If both
/// refresh, the second presents a token the first has already spent, the
/// server revokes the family, and a laptop holding meetings that have not been
/// uploaded is signed out. So exactly one refresh must happen.
#[tokio::test]
async fn two_callers_never_spend_two_refresh_tokens() {
    let root = scratch("race");
    // A token that is already too old to use, so both callers want a new one.
    let server = Server::new(0);
    let (account, _) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");

    let (first, second) = tokio::join!(account.access_token(), account.access_token());

    let first = first.expect("the first caller gets a token");
    let second = second.expect("the second caller gets a token");
    assert_eq!(first, second, "the two callers hold different sessions");

    let presented = server.calls().presented.clone();
    assert_eq!(
        presented.len(),
        1,
        "a refresh token was presented twice, which revokes the family: {presented:?}"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A token still comfortably alive is used as it is: renewing on every request
/// would spend a single-use token per call and multiply the chances of the
/// race above.
#[tokio::test]
async fn a_live_token_is_not_renewed() {
    let root = scratch("live");
    let server = Server::new(3600);
    let (account, _) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");

    for _ in 0..5 {
        account.access_token().await.expect("a token");
    }

    assert!(
        server.calls().presented.is_empty(),
        "a live session was renewed for nothing"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A renewal is not a reason to ask the server who this is again. It happens
/// every few minutes; a display name does not change that often.
#[tokio::test]
async fn a_renewal_does_not_ask_who_this_is_again() {
    let root = scratch("profile");
    let server = Server::new(0);
    let (account, _) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");
    let after_sign_in = server.calls().profiles;

    account.access_token().await.expect("renews");

    assert_eq!(server.calls().profiles, after_sign_in);
    assert!(
        account.identified().await.is_some(),
        "the renewal lost who was signed in"
    );

    let _ = std::fs::remove_dir_all(&root);
}

/// A refused token is dead. Keeping it would make every later attempt present
/// it again - which is the thing that revokes a family - and would show the
/// same failure for ever instead of a sign-in screen.
#[tokio::test]
async fn a_refused_token_is_forgotten() {
    let root = scratch("refused");
    let server = Server::new(3600);
    let (account, path) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");

    server.refuse_refresh();
    account.restore().await.expect_err("the session is over");

    assert_eq!(stored(&path), None, "a dead token was kept");
    assert!(!account.is_linked());

    let _ = std::fs::remove_dir_all(&root);
}

/// A machine somebody asked to sign out of must not keep a credential.
#[tokio::test]
async fn signing_out_leaves_nothing_behind() {
    let root = scratch("signout");
    let server = Server::new(3600);
    let (account, path) = account(&server, &root);
    account
        .sign_in("awa@example.invalid", "pw")
        .await
        .expect("signs in");

    account.sign_out().await.expect("signs out");

    assert_eq!(stored(&path), None);
    assert!(!account.is_linked());
    assert!(account.identified().await.is_none());

    let _ = std::fs::remove_dir_all(&root);
}
