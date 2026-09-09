"""Account and session logic (EF-01 to EF-03, section 17.3).

Kept out of the routes so it can be tested against a database without going
through HTTP, and so the rules live in one place rather than being restated in
every endpoint (CLAUDE.md section 6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import set_current_organization
from app.email import EmailDeliveryError, EmailProvider
from app.logging import get_logger
from app.models import ActorType, AuditLog, Organization, RefreshToken, Role, User
from app.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.services import verification
from app.uuid7 import uuid7

logger = get_logger(__name__)


class AuthError(Exception):
    """Authentication failed. Carries a stable code for the API layer."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class IssuedSession:
    """A freshly minted token pair and who it belongs to."""

    access_token: str
    refresh_token: str
    expires_in: int
    user: User
    organization: Organization


async def register(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider | None = None,
    full_name: str,
    organization_name: str,
    password: str,
    email: str | None,
    phone: str | None,
    locale: str,
    timezone: str,
) -> IssuedSession:
    """EF-01: create an organization and its first user, who becomes Owner.

    The caller must supply an *unscoped* session: there is no organization to
    scope to until this function creates one.

    An `email_provider` also sends the EF-02 verification link. A failure to
    deliver it does not fail the signup: the account is real, the address is
    simply not proven yet, and there is a resend endpoint for that. Rolling the
    registration back because the mail provider hiccuped would take the whole
    signup funnel down with it.
    """
    normalised_email = email.lower().strip() if email else None

    # A plain SELECT here would be filtered by RLS and always return nothing,
    # which would let the same address register twice. The lookup function
    # crosses tenants deliberately and returns only what is needed.
    existing = await session.execute(
        text("SELECT id FROM auth_lookup_user(:email, :phone)"),
        {"email": normalised_email, "phone": phone},
    )
    if existing.first() is not None:
        # The same message and code whether or not the account exists, so the
        # endpoint cannot be used to enumerate customers.
        raise AuthError("ACCOUNT_EXISTS", "this account cannot be created")

    # Creating a tenant is the one write that cannot already be scoped to one.
    # The identifier is generated here rather than by the database, so the
    # session can be scoped to the organization *before* it is inserted: the
    # policy's WITH CHECK then passes on its own terms. No privilege
    # escalation, no exemption — the row is written under the same rule as
    # every other row in the system.
    organization_id = uuid7()
    await set_current_organization(session, organization_id)

    organization = Organization(
        id=organization_id,
        name=organization_name.strip(),
        market=settings.default_market,
        default_language=locale,
    )
    session.add(organization)
    await session.flush()

    user = User(
        id=uuid7(),
        organization_id=organization.id,
        email=normalised_email,
        phone=phone,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        # Whoever creates the organization owns it; only an Owner can delete it
        # or change the plan.
        role=Role.OWNER.value,
        locale=locale,
        timezone=timezone,
    )
    session.add(user)
    await session.flush()

    await _audit(
        session,
        organization_id=organization.id,
        actor_id=user.id,
        action="organization.created",
        target_type="organization",
        target_id=organization.id,
    )

    if email_provider is not None:
        try:
            await verification.send_verification(
                session, settings=settings, email_provider=email_provider, user=user
            )
        except EmailDeliveryError:
            # Logged, not raised. See the docstring: an account without its
            # verification mail is recoverable, a failed signup is a lost
            # customer.
            logger.warning("verification_email_not_sent")

    return await _issue_session(session, settings=settings, user=user, organization=organization)


async def authenticate(
    session: AsyncSession,
    *,
    settings: Settings,
    password: str,
    email: str | None,
    phone: str | None,
) -> IssuedSession:
    """Sign a user in.

    The session must be unscoped: the organization is not known until the user
    is found, which is precisely what this resolves.
    """
    normalised_email = email.lower().strip() if email else None

    # Signing in is inherently cross-tenant: the organization is unknown until
    # the user is found. auth_lookup_user is the narrow, audited way through
    # RLS; it returns four columns and no way to enumerate accounts.
    found = (
        await session.execute(
            text(
                "SELECT id, organization_id, password_hash, revoked_at "
                "FROM auth_lookup_user(:email, :phone)"
            ),
            {"email": normalised_email, "phone": phone},
        )
    ).first()

    if found is None:
        # Hash anyway so a missing account and a wrong password take the same
        # time; otherwise the endpoint answers "does this address exist?".
        verify_password(password, _DUMMY_HASH)
        raise AuthError("INVALID_CREDENTIALS", "invalid credentials")

    if not verify_password(password, found.password_hash):
        raise AuthError("INVALID_CREDENTIALS", "invalid credentials")

    if found.revoked_at is not None:
        raise AuthError("ACCOUNT_REVOKED", "this account has been revoked")

    # From here the tenant is known, so everything runs under RLS like the
    # rest of the API.
    await set_current_organization(session, found.organization_id)
    user = await session.get(User, found.id)
    organization = await session.get(Organization, found.organization_id)
    if user is None or organization is None:  # pragma: no cover - unreachable via FK
        raise AuthError("INVALID_CREDENTIALS", "invalid credentials")

    await _audit(
        session,
        organization_id=organization.id,
        actor_id=user.id,
        action="session.created",
        target_type="user",
        target_id=user.id,
    )
    return await _issue_session(session, settings=settings, user=user, organization=organization)


async def refresh(
    session: AsyncSession, *, settings: Settings, presented_token: str
) -> IssuedSession:
    """Rotate a refresh token.

    Using a token consumes it and issues a replacement. Presenting one that has
    already been used means the token leaked — the legitimate holder and an
    attacker now both have one — so the entire family is revoked and both are
    forced to sign in again (section 17.3).
    """
    token_hash = hash_refresh_token(presented_token)
    # Same reason as signing in: the tenant is unknown until the token is
    # found, so the organization is resolved first and everything after runs
    # scoped.
    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_refresh_token(:hash)"),
            {"hash": token_hash},
        )
    ).first()
    if located is None:
        raise AuthError("INVALID_REFRESH_TOKEN", "invalid refresh token")

    await set_current_organization(session, located.organization_id)
    stored = await session.get(RefreshToken, located.id)
    if stored is None:  # pragma: no cover - the row was just located
        raise AuthError("INVALID_REFRESH_TOKEN", "invalid refresh token")

    now = datetime.now(UTC)

    if stored.used_at is not None:
        await _revoke_family(session, stored.family_id, reason="reuse_detected")
        await _audit(
            session,
            organization_id=stored.organization_id,
            actor_id=stored.user_id,
            action="session.reuse_detected",
            target_type="refresh_token_family",
            target_id=stored.family_id,
            reason="a refresh token was presented twice; the family was revoked",
        )
        logger.warning("refresh_token_reuse", organization_id=str(stored.organization_id))
        # Commit before raising. Raising inside the caller's transaction would
        # roll the revocation back, and the whole point of detecting reuse is
        # that the revocation outlives the failed request — otherwise the
        # attacker's token survives the very event that was meant to kill it.
        await session.commit()
        raise AuthError("REFRESH_TOKEN_REUSED", "this session has been revoked")

    if stored.revoked_at is not None:
        raise AuthError("INVALID_REFRESH_TOKEN", "invalid refresh token")

    if stored.expires_at <= now:
        raise AuthError("REFRESH_TOKEN_EXPIRED", "the refresh token has expired")

    user = await session.get(User, stored.user_id)
    if user is None or user.revoked_at is not None:
        # EF-03: a revoked member loses access at their next refresh, which is
        # at most one access-token lifetime away. Committed for the same reason
        # as above: the revocation must survive the failed request.
        await _revoke_family(session, stored.family_id, reason="user_revoked")
        await session.commit()
        raise AuthError("ACCOUNT_REVOKED", "this account has been revoked")

    organization = await session.get(Organization, stored.organization_id)
    if organization is None:  # pragma: no cover - foreign key makes this unreachable
        raise AuthError("INVALID_REFRESH_TOKEN", "invalid refresh token")

    stored.used_at = now
    return await _issue_session(
        session,
        settings=settings,
        user=user,
        organization=organization,
        family_id=stored.family_id,
    )


async def revoke_session(session: AsyncSession, *, presented_token: str) -> None:
    """Sign out: revoke the whole family this token belongs to.

    Logging out on one machine should not leave the other tokens from that
    login usable.
    """
    token_hash = hash_refresh_token(presented_token)
    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_refresh_token(:hash)"),
            {"hash": token_hash},
        )
    ).first()
    if located is None:
        # Logging out with an unknown token is not an error worth reporting:
        # the caller wanted no session, and has none.
        return

    await set_current_organization(session, located.organization_id)
    stored = await session.get(RefreshToken, located.id)
    if stored is None:  # pragma: no cover - the row was just located
        return
    await _revoke_family(session, stored.family_id, reason="logout")
    await _audit(
        session,
        organization_id=stored.organization_id,
        actor_id=stored.user_id,
        action="session.revoked",
        target_type="refresh_token_family",
        target_id=stored.family_id,
    )


async def _issue_session(
    session: AsyncSession,
    *,
    settings: Settings,
    user: User,
    organization: Organization,
    family_id: uuid.UUID | None = None,
) -> IssuedSession:
    """Mint an access token and a stored refresh token."""
    access = create_access_token(
        settings=settings,
        user_id=user.id,
        organization_id=organization.id,
        role=user.role,
    )
    raw_refresh = new_refresh_token()

    session.add(
        RefreshToken(
            id=uuid7(),
            organization_id=organization.id,
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            # A rotated token inherits its predecessor's family, so a leak can
            # be traced back to one login rather than to one request.
            family_id=family_id or uuid7(),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
    )

    return IssuedSession(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user=user,
        organization=organization,
    )


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, *, reason: str) -> None:
    """Revoke every token descended from one login."""
    now = datetime.now(UTC)
    tokens = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).all()
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = reason


async def _audit(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Record an action that changed access, money or data lifetime."""
    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization_id,
            actor_id=actor_id,
            actor_type=ActorType.USER.value if actor_id else ActorType.SYSTEM.value,
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
    )


# A real Argon2id hash of a value nobody knows, used to spend the same time on
# a missing account as on a wrong password.
_DUMMY_HASH = hash_password("novabrief-timing-equaliser")
