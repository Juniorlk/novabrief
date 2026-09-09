"""Proving that an address belongs to whoever signed up (EF-02).

Same three rules as the invitation and reset links, for the same reasons: the
token is random and only its SHA-256 is stored, it is single-use and
time-boxed, and every refusal reads the same so the endpoint cannot be used to
learn whether a link ever existed.

What differs is the deadline. A reset link lives 30 minutes because it *is* a
password; a verification link proves an address and grants nothing on its own,
so a window short enough to expire while someone is in a meeting would only
generate support tickets. Twenty-four hours, and a way to ask for another.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import set_current_organization
from app.email import EmailProvider, Message
from app.logging import get_logger
from app.models import ActorType, AuditLog, EmailVerification, User
from app.security import hash_refresh_token, new_refresh_token
from app.uuid7 import uuid7

logger = get_logger(__name__)

EMAIL_VERIFICATION_TTL = timedelta(hours=24)


class VerificationError(Exception):
    """A verification failed. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def send_verification(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider,
    user: User,
) -> str | None:
    """Issue a link and email it. Returns the token, or None if there is nothing to verify.

    Signing up with a phone number alone is allowed by EF-01, and there is then
    no address to prove — so this is a no-op rather than an error.

    The session must already be scoped to the user's organization.
    """
    if not user.email:
        return None

    token = new_refresh_token()
    session.add(
        EmailVerification(
            id=uuid7(),
            organization_id=user.organization_id,
            user_id=user.id,
            email=user.email,
            token_hash=hash_refresh_token(token),
            expires_at=datetime.now(UTC) + EMAIL_VERIFICATION_TTL,
        )
    )

    link = f"{settings.web_base_url}/verify-email?token={token}"
    await email_provider.send(
        Message(
            to=user.email,
            subject="NovaBrief - confirmez votre adresse email",
            text=(
                f"Bienvenue sur NovaBrief, {user.full_name}.\n\n"
                f"Confirmez votre adresse pour activer les notifications et la "
                f"reinitialisation de mot de passe : {link}\n\n"
                f"Ce lien expire dans 24 heures.\n"
                f"Si vous n'avez pas cree de compte NovaBrief, ignorez ce message."
            ),
        )
    )
    return token


async def confirm(session: AsyncSession, *, token: str) -> User:
    """EF-02: consume the link and mark the address proven.

    The session must be unscoped: the organization is only known once the row
    is found.
    """
    located = (
        await session.execute(
            text("SELECT id, organization_id FROM auth_lookup_email_verification(:hash)"),
            {"hash": hash_refresh_token(token)},
        )
    ).first()
    if located is None:
        raise VerificationError("INVALID_VERIFICATION_TOKEN", "this link is not valid")

    await set_current_organization(session, located.organization_id)
    verification = await session.get(EmailVerification, located.id)
    if verification is None:  # pragma: no cover - the row was just located
        raise VerificationError("INVALID_VERIFICATION_TOKEN", "this link is not valid")

    now = datetime.now(UTC)
    if verification.used_at is not None or verification.expires_at <= now:
        # One message for expired and for already-used. EF-02 asks for a clear
        # refusal, and which of the two it was is not the user's problem.
        raise VerificationError(
            "INVALID_VERIFICATION_TOKEN", "this link has expired or was already used"
        )

    user = await session.get(User, verification.user_id)
    if user is None:  # pragma: no cover - foreign key makes this unreachable
        raise VerificationError("INVALID_VERIFICATION_TOKEN", "this link is not valid")

    if user.email != verification.email:
        # The address changed after the link was issued. Confirming would mark
        # the *new* address proven on the strength of a mail sent to the old
        # one, which is how an account takeover survives an address change.
        raise VerificationError(
            "INVALID_VERIFICATION_TOKEN", "this link no longer matches the account"
        )

    verification.used_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=verification.organization_id,
            actor_id=user.id,
            actor_type=ActorType.USER.value,
            action="email.verified",
            target_type="user",
            target_id=user.id,
        )
    )
    logger.info("email_verified")
    return user


async def resend(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider,
    user: User,
) -> None:
    """EF-02: ask for another link.

    Refused once the address is proven: re-issuing a live token for an already
    verified account creates a credential nobody needs.
    """
    if user.email_verified_at is not None:
        raise VerificationError("EMAIL_ALREADY_VERIFIED", "this address is already verified")
    if not user.email:
        raise VerificationError("NO_EMAIL_ON_ACCOUNT", "this account has no email address")

    await send_verification(session, settings=settings, email_provider=email_provider, user=user)
