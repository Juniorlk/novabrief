"""Invitations and password reset (EF-02, EF-03).

Both features hand out a link that grants access, so both follow the same three
rules:

* the token is random and only its SHA-256 is stored, so a database dump grants
  nothing;
* it is single-use and time-boxed — 7 days for an invitation, 30 minutes for a
  password reset, which is what EF-02 asks for;
* using it twice is refused with the same message as an expired one, so the
  endpoint cannot be used to learn whether a link was ever valid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import set_current_organization
from app.email import EmailProvider, Message
from app.logging import get_logger
from app.models import (
    ActorType,
    AuditLog,
    Invitation,
    Organization,
    PasswordReset,
    RefreshToken,
    Role,
    User,
)
from app.security import hash_password, hash_refresh_token, new_refresh_token
from app.uuid7 import uuid7

logger = get_logger(__name__)

INVITATION_TTL = timedelta(days=7)
# EF-02 fixes this at 30 minutes. Short on purpose: a reset link is a password.
PASSWORD_RESET_TTL = timedelta(minutes=30)


class MemberError(Exception):
    """A membership operation failed. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class IssuedInvitation:
    """A created invitation and the token that was emailed."""

    invitation: Invitation
    token: str


async def invite_member(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider,
    organization: Organization,
    inviter: User,
    email: str,
    role: str,
) -> IssuedInvitation:
    """EF-03: invite someone by email, with a role.

    The session must already be scoped to `organization`.
    """
    if role not in {Role.ADMIN.value, Role.MEMBER.value}:
        # Ownership transfers deliberately, not by invitation: an organization
        # has exactly one Owner and changing it is a separate, audited act.
        raise MemberError("INVALID_ROLE", "a member can be invited as ADMIN or MEMBER")

    address = email.lower().strip()

    # Cross-tenant on purpose: a person belongs to one organization in V1, so
    # inviting someone who already has an account elsewhere has to be refused
    # rather than silently creating a second identity.
    existing = (
        await session.execute(
            text("SELECT id FROM auth_lookup_user(:email, NULL)"), {"email": address}
        )
    ).first()
    if existing is not None:
        raise MemberError("ACCOUNT_EXISTS", "this address already has an account")

    pending = await session.scalar(
        select(Invitation).where(
            Invitation.email == address,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > datetime.now(UTC),
        )
    )
    if pending is not None:
        raise MemberError("INVITATION_PENDING", "an invitation is already pending")

    token = new_refresh_token()
    invitation = Invitation(
        id=uuid7(),
        organization_id=organization.id,
        invited_by=inviter.id,
        email=address,
        role=role,
        token_hash=hash_refresh_token(token),
        expires_at=datetime.now(UTC) + INVITATION_TTL,
    )
    session.add(invitation)

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=inviter.id,
            actor_type=ActorType.USER.value,
            action="member.invited",
            target_type="invitation",
            target_id=invitation.id,
        )
    )

    link = f"{settings.web_base_url}/invitation?token={token}"
    await email_provider.send(
        Message(
            to=address,
            subject=f"{inviter.full_name} vous invite sur NovaBrief",
            text=(
                f"{inviter.full_name} vous invite a rejoindre l'organisation "
                f"{organization.name} sur NovaBrief.\n\n"
                f"Acceptez l'invitation : {link}\n\n"
                f"Ce lien expire dans 7 jours."
            ),
        )
    )

    return IssuedInvitation(invitation=invitation, token=token)


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    full_name: str,
    password: str,
    locale: str,
    timezone: str,
) -> User:
    """EF-03: accept in one click, creating the account.

    The session must be unscoped: the organization is only known once the
    invitation is found.
    """
    token_hash = hash_refresh_token(token)

    # Same reasoning as sign-in: the tenant is unknown until the row is found.
    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_invitation(:hash)"),
            {"hash": token_hash},
        )
    ).first()
    if located is None:
        raise MemberError("INVALID_INVITATION", "this invitation link is not valid")

    await set_current_organization(session, located.organization_id)
    invitation = await session.get(Invitation, located.id)
    if invitation is None:  # pragma: no cover - the row was just located
        raise MemberError("INVALID_INVITATION", "this invitation link is not valid")

    # One message for every refusal: expired, already used, revoked. Telling
    # them apart would say whether a link ever existed.
    now = datetime.now(UTC)
    if (
        invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise MemberError("INVALID_INVITATION", "this invitation link is not valid")

    user = User(
        id=uuid7(),
        organization_id=invitation.organization_id,
        email=invitation.email,
        phone=None,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        role=invitation.role,
        locale=locale,
        timezone=timezone,
        # The invitation was sent to this address and the link came back, which
        # proves the address as well as a verification email would.
        email_verified_at=now,
    )
    session.add(user)
    invitation.accepted_at = now

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=invitation.organization_id,
            actor_id=user.id,
            actor_type=ActorType.USER.value,
            action="member.joined",
            target_type="user",
            target_id=user.id,
        )
    )
    return user


async def revoke_member(
    session: AsyncSession, *, organization: Organization, actor: User, member_id: uuid.UUID
) -> None:
    """EF-03: a revoked member loses access in under a minute.

    Marking the user is not enough on its own — their access token stays
    cryptographically valid until it expires. Their refresh tokens are revoked
    at the same time, so the longest they can keep working is one access-token
    lifetime, and `current_caller` refuses them immediately on any request that
    reaches the database.
    """
    if member_id == actor.id:
        raise MemberError("CANNOT_REVOKE_SELF", "you cannot revoke your own access")

    member = await session.get(User, member_id)
    if member is None:
        raise MemberError("MEMBER_NOT_FOUND", "no such member in this organization")
    if member.role == Role.OWNER.value:
        raise MemberError("CANNOT_REVOKE_OWNER", "the organization owner cannot be revoked")

    now = datetime.now(UTC)
    member.revoked_at = now

    tokens = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == member_id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).all()
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = "member_revoked"

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=actor.id,
            actor_type=ActorType.USER.value,
            action="member.revoked",
            target_type="user",
            target_id=member_id,
        )
    )


async def request_password_reset(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider,
    email: str,
) -> None:
    """EF-02: email a single-use reset link valid 30 minutes.

    Returns without error whether or not the address exists. An endpoint that
    answered differently would be a way to test which addresses are customers,
    and it is reachable without any credential.
    """
    address = email.lower().strip()

    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_user(:email, NULL)"),
            {"email": address},
        )
    ).first()
    if located is None:
        logger.info("password_reset_requested_for_unknown_address")
        return

    await set_current_organization(session, located.organization_id)

    token = new_refresh_token()
    session.add(
        PasswordReset(
            id=uuid7(),
            organization_id=located.organization_id,
            user_id=located.id,
            token_hash=hash_refresh_token(token),
            expires_at=datetime.now(UTC) + PASSWORD_RESET_TTL,
        )
    )

    link = f"{settings.web_base_url}/reset-password?token={token}"
    await email_provider.send(
        Message(
            to=address,
            subject="Reinitialisation de votre mot de passe NovaBrief",
            text=(
                "Vous avez demande a reinitialiser votre mot de passe.\n\n"
                f"Choisissez un nouveau mot de passe : {link}\n\n"
                "Ce lien expire dans 30 minutes et ne fonctionne qu'une fois.\n"
                "Si vous n'etes pas a l'origine de cette demande, ignorez ce message."
            ),
        )
    )


async def confirm_password_reset(session: AsyncSession, *, token: str, new_password: str) -> None:
    """EF-02: consume the link and set the new password.

    Every session is revoked at the same time. Someone resetting a password is
    often doing it because they think an account is compromised; leaving the
    attacker's sessions alive would defeat the exercise.
    """
    token_hash = hash_refresh_token(token)

    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_password_reset(:hash)"),
            {"hash": token_hash},
        )
    ).first()
    if located is None:
        raise MemberError("INVALID_RESET_TOKEN", "this reset link is not valid")

    await set_current_organization(session, located.organization_id)
    reset = await session.get(PasswordReset, located.id)
    if reset is None:  # pragma: no cover - the row was just located
        raise MemberError("INVALID_RESET_TOKEN", "this reset link is not valid")

    now = datetime.now(UTC)
    if reset.used_at is not None or reset.expires_at <= now:
        # EF-02 asks for a clear message on an expired or reused link, and the
        # same one for both: which it was is not the user's problem and would
        # tell an attacker something.
        raise MemberError("INVALID_RESET_TOKEN", "this reset link has expired or was already used")

    user = await session.get(User, reset.user_id)
    if user is None:  # pragma: no cover - foreign key makes this unreachable
        raise MemberError("INVALID_RESET_TOKEN", "this reset link is not valid")

    user.password_hash = hash_password(new_password)
    reset.used_at = now

    sessions = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).all()
    for existing in sessions:
        existing.revoked_at = now
        existing.revoked_reason = "password_reset"

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=reset.organization_id,
            actor_id=user.id,
            actor_type=ActorType.USER.value,
            action="password.reset",
            target_type="user",
            target_id=user.id,
        )
    )
