"""Password hashing and token minting.

Part of T-14: token expiry and rotation. These are the properties that fail
silently — a token that never expires still works, and a signature check that
accepts the wrong algorithm still returns claims.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.security import (
    ALGORITHM,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)


def _key_pair() -> tuple[str, str]:
    """A throwaway RS256 key pair for the tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


@pytest.fixture(scope="module")
def settings() -> Settings:
    private, public = _key_pair()
    return Settings(jwt_private_key=private, jwt_public_key=public)


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("un mot de passe assez long")

    assert verify_password("un mot de passe assez long", stored)


def test_a_wrong_password_is_rejected() -> None:
    stored = hash_password("un mot de passe assez long")

    assert not verify_password("un autre mot de passe", stored)


def test_the_hash_never_contains_the_password() -> None:
    """A database dump must not be a password list."""
    stored = hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in stored
    assert stored.startswith("$argon2id$")


def test_the_same_password_hashes_differently_every_time() -> None:
    """Salting: two accounts with the same password must not look alike."""
    first = hash_password("le meme mot de passe")
    second = hash_password("le meme mot de passe")

    assert first != second


def test_a_malformed_hash_is_rejected_rather_than_crashing() -> None:
    assert not verify_password("anything", "not-a-hash")


# --------------------------------------------------------------------------
# Access tokens
# --------------------------------------------------------------------------


def test_an_access_token_round_trips_its_claims(settings: Settings) -> None:
    user_id, org_id = uuid.uuid4(), uuid.uuid4()

    token = create_access_token(
        settings=settings, user_id=user_id, organization_id=org_id, role="ADMIN"
    )
    claims = decode_access_token(settings=settings, token=token)

    assert claims.user_id == user_id
    assert claims.organization_id == org_id
    assert claims.role == "ADMIN"


def test_an_expired_token_is_refused(settings: Settings) -> None:
    """The 15-minute lifetime is what bounds a revoked member's access."""
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    token = create_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role="MEMBER",
        now=long_ago,
    )

    with pytest.raises(TokenError, match="expired"):
        decode_access_token(settings=settings, token=token)


def test_the_token_expires_after_the_configured_ttl(settings: Settings) -> None:
    issued = datetime.now(UTC)
    token = create_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role="MEMBER",
        now=issued,
    )

    claims = decode_access_token(settings=settings, token=token)
    lifetime = claims.expires_at - issued
    assert abs(lifetime - timedelta(minutes=settings.jwt_access_ttl_minutes)) < timedelta(seconds=2)


def test_a_token_signed_by_another_key_is_refused(settings: Settings) -> None:
    """The signature is the whole point; a valid shape is not enough."""
    other_private, _ = _key_pair()
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org": str(uuid.uuid4()),
            "role": "OWNER",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            "typ": "access",
        },
        other_private,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(settings=settings, token=forged)


def test_an_unsigned_token_is_refused(settings: Settings) -> None:
    """The classic `alg: none` downgrade. The algorithm is pinned, so it fails."""
    unsigned = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org": str(uuid.uuid4()),
            "role": "OWNER",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            "typ": "access",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        decode_access_token(settings=settings, token=unsigned)


def test_a_token_of_another_type_cannot_be_replayed_as_access(settings: Settings) -> None:
    """`typ` stops a token minted for one purpose being spent on another."""
    other_purpose = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org": str(uuid.uuid4()),
            "role": "OWNER",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            "typ": "ws_ticket",
        },
        settings.require_jwt_private_key(),
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError, match="not an access token"):
        decode_access_token(settings=settings, token=other_purpose)


def test_garbage_is_refused_without_crashing(settings: Settings) -> None:
    with pytest.raises(TokenError):
        decode_access_token(settings=settings, token="not.a.token")


def test_minting_without_a_key_fails_loudly() -> None:
    """Better a clear refusal than tokens that silently die on restart."""
    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY"):
        create_access_token(
            settings=Settings(),
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            role="MEMBER",
        )


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------


def test_refresh_tokens_are_unique() -> None:
    tokens = {new_refresh_token() for _ in range(1_000)}

    assert len(tokens) == 1_000


def test_a_refresh_token_is_stored_only_as_a_hash() -> None:
    token = new_refresh_token()
    stored = hash_refresh_token(token)

    assert token not in stored
    assert len(stored) == 64  # SHA-256, hex encoded


def test_hashing_a_refresh_token_is_deterministic() -> None:
    """Lookup happens by hash, so the same token must always hash the same."""
    token = new_refresh_token()

    assert hash_refresh_token(token) == hash_refresh_token(token)


# --------------------------------------------------------------------------
# Carrying a PEM through the environment
# --------------------------------------------------------------------------


def test_a_pem_written_on_one_line_is_restored() -> None:
    r"""Deployments write the key with \n, because env_file cannot carry newlines.

    Docker Compose's parser does not reliably keep a multi-line value, so every
    deployment escapes the key. If this stops working the API starts, accepts
    requests, and fails to sign the first token - which looks like a key
    problem rather than a parsing one.
    """
    escaped = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"

    settings = Settings(jwt_private_key=escaped)

    assert settings.require_jwt_private_key() == (
        "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"
    )


def test_a_pem_with_real_newlines_is_left_alone() -> None:
    """A key exported straight from a shell must keep working."""
    real = "-----BEGIN PUBLIC KEY-----\nMIIabc\n-----END PUBLIC KEY-----"

    settings = Settings(jwt_public_key=real)

    assert settings.require_jwt_public_key() == real


def test_an_escaped_key_still_signs_and_verifies() -> None:
    """The property that matters, end to end rather than by string comparison."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    settings = Settings(
        jwt_private_key=private_pem.replace("\n", "\n"),
        jwt_public_key=public_pem.replace("\n", "\n"),
    )

    token = create_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role="OWNER",
    )
    claims = decode_access_token(settings=settings, token=token)

    assert claims.role == "OWNER"
