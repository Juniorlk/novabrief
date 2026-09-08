"""Password hashing and token minting (section 17.3, section 21.1).

Three decisions carry most of the weight here:

* **Argon2id** for passwords. It is memory-hard, so an attacker with GPUs gains
  far less than against bcrypt or PBKDF2.
* **RS256** for access tokens. Asymmetric signing means the workers and, later,
  any other service can verify a token with the public key alone; only the API
  holds the private key. An HS256 secret would have to be shared with every
  verifier, and a shared secret can mint tokens as well as check them.
* **Refresh tokens are stored hashed and rotate on use.** A database dump must
  not hand out working sessions, and a token presented twice means it leaked.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import Settings

ALGORITHM: Final = "RS256"

# OWASP's second recommended Argon2id profile: 19 MiB, two iterations. Memory
# is what actually costs an attacker, so it is raised before the time cost.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

# EF-01. Length beats composition rules: a long passphrase is both stronger and
# easier to remember than eight characters with a digit and a symbol.
MIN_PASSWORD_LENGTH: Final = 10

# Bytes of entropy in a refresh token. 32 bytes is 256 bits, far beyond
# guessing, and the value never has to be typed by a human.
_REFRESH_TOKEN_BYTES: Final = 32


class TokenError(Exception):
    """A token could not be verified."""


@dataclass(frozen=True)
class AccessClaims:
    """What an access token asserts (section 17.3)."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: str
    expires_at: datetime


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against its hash.

    Returns a boolean rather than raising: a caller that has to catch three
    exception types to answer "is this the right password" eventually catches
    the wrong one, or none.
    """
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash was made with weaker parameters than current."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(
    *,
    settings: Settings,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role: str,
    now: datetime | None = None,
) -> str:
    """Mint a short-lived access token.

    Short-lived on purpose: the token itself carries the organization and role,
    so a revoked member would otherwise keep their access until it expired.
    Fifteen minutes is what bounds that window (EF-03 asks for under a minute
    on refresh, which is where revocation is actually checked).
    """
    issued = now or datetime.now(UTC)
    expires = issued + timedelta(minutes=settings.jwt_access_ttl_minutes)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(organization_id),
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        # Distinguishes an access token from any other RS256 token the platform
        # might sign later, so one can never be replayed as the other.
        "typ": "access",
    }
    return jwt.encode(payload, settings.require_jwt_private_key(), algorithm=ALGORITHM)


def decode_access_token(*, settings: Settings, token: str) -> AccessClaims:
    """Verify an access token and return its claims.

    The algorithm is pinned. Accepting whatever the token's header asks for is
    how a verifier gets talked into `none`, or into treating the public key as
    an HMAC secret.
    """
    try:
        payload = jwt.decode(
            token,
            settings.require_jwt_public_key(),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        message = "the access token has expired"
        raise TokenError(message) from exc
    except jwt.InvalidTokenError as exc:
        message = "the access token is invalid"
        raise TokenError(message) from exc

    if payload.get("typ") != "access":
        message = "this token is not an access token"
        raise TokenError(message)

    try:
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]),
            role=str(payload["role"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        message = "the access token is missing a required claim"
        raise TokenError(message) from exc


def new_refresh_token() -> str:
    """Generate an opaque refresh token."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token for storage.

    SHA-256 rather than Argon2, deliberately. The token is 256 bits of
    randomness we generated, not a human-chosen password, so there is nothing
    to brute-force and no reason to pay a memory-hard hash on every refresh.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two secrets without leaking their contents through timing."""
    return secrets.compare_digest(left, right)
