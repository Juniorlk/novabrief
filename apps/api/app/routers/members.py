"""Member and password endpoints (EF-02, EF-03).

Thin routes over `app.services.members`, same as the auth router: HTTP in,
stable error codes out, rules stated once in the service.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from app.config import Settings, get_settings
from app.deps import AdminCaller, CurrentCaller, ScopedSession, UnscopedSession
from app.email import EmailDeliveryError, EmailProvider
from app.errors import ProblemError
from app.models import User
from app.services import members
from schemas.auth import (
    AcceptInvitationRequest,
    InvitationCreated,
    InviteMemberRequest,
    MemberSummary,
    PasswordResetConfirm,
    PasswordResetRequest,
)

router = APIRouter(tags=["members"])

# Integers rather than Starlette constants: the framework has renamed several
# of them between versions, and an HTTP status number is the stabler spelling.
_STATUS_FOR_CODE = {
    "ACCOUNT_EXISTS": 409,
    "INVITATION_PENDING": 409,
    "INVALID_ROLE": 422,
    "INVALID_INVITATION": 400,
    "INVALID_RESET_TOKEN": 400,
    "MEMBER_NOT_FOUND": 404,
    "CANNOT_REVOKE_SELF": 409,
    "CANNOT_REVOKE_OWNER": 409,
}


def _as_problem(error: members.MemberError) -> ProblemError:
    return ProblemError(
        status_code=_STATUS_FOR_CODE.get(error.code, 400),
        code=error.code,
        title=str(error),
    )


def email_provider(request: Request) -> EmailProvider:
    """The configured email provider, attached to the app at startup."""
    provider: EmailProvider | None = getattr(request.app.state, "email_provider", None)
    if provider is None:  # pragma: no cover - only if the app was built wrongly
        raise ProblemError(
            status_code=503,
            code="EMAIL_UNAVAILABLE",
            title="Email delivery is not configured.",
        )
    return provider


EmailSender = Annotated[EmailProvider, Depends(email_provider)]


@router.get(
    "/organizations/current/members",
    response_model=list[MemberSummary],
    summary="List the organization's members",
)
async def list_members(caller: CurrentCaller, session: ScopedSession) -> list[MemberSummary]:
    """RLS scopes this to the caller's organization; no filter is written here."""
    rows = (await session.scalars(select(User).order_by(User.created_at))).all()
    return [
        MemberSummary(
            id=member.id,
            email=member.email,
            full_name=member.full_name,
            role=member.role,  # type: ignore[arg-type]
            revoked=member.revoked_at is not None,
            created_at=member.created_at,
        )
        for member in rows
    ]


@router.post(
    "/organizations/current/members",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a member",
)
async def invite(
    payload: InviteMemberRequest,
    caller: AdminCaller,
    session: ScopedSession,
    sender: EmailSender,
    settings: Annotated[Settings, Depends(get_settings)],
) -> InvitationCreated:
    """EF-03. Admins and Owners only."""
    try:
        issued = await members.invite_member(
            session,
            settings=settings,
            email_provider=sender,
            organization=caller.organization,
            inviter=caller.user,
            email=payload.email,
            role=payload.role,
        )
    except members.MemberError as error:
        raise _as_problem(error) from error
    except EmailDeliveryError as error:
        # The invitation row is rolled back with the transaction, so a failed
        # send leaves no dangling token and the caller can simply retry.
        raise ProblemError(
            status_code=502,
            code="EMAIL_DELIVERY_FAILED",
            title="The invitation could not be sent.",
        ) from error

    return InvitationCreated(
        id=issued.invitation.id,
        email=issued.invitation.email,
        role=issued.invitation.role,  # type: ignore[arg-type]
        expires_at=issued.invitation.expires_at,
    )


@router.post(
    "/auth/invitations/accept",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Accept an invitation",
)
async def accept(payload: AcceptInvitationRequest, session: UnscopedSession) -> None:
    """EF-03: one click, which creates the account.

    Unscoped: the organization is only known once the invitation is found.
    """
    try:
        await members.accept_invitation(
            session,
            token=payload.token,
            full_name=payload.full_name,
            password=payload.password,
            locale=payload.locale,
            timezone=payload.timezone,
        )
    except members.MemberError as error:
        raise _as_problem(error) from error


@router.delete(
    "/organizations/current/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a member",
)
async def revoke(member_id: uuid.UUID, caller: AdminCaller, session: ScopedSession) -> None:
    """EF-03: access ends in under a minute."""
    try:
        await members.revoke_member(
            session,
            organization=caller.organization,
            actor=caller.user,
            member_id=member_id,
        )
    except members.MemberError as error:
        raise _as_problem(error) from error


@router.post(
    "/auth/password/reset",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset link",
)
async def request_reset(
    payload: PasswordResetRequest,
    session: UnscopedSession,
    sender: EmailSender,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """EF-02.

    Always 202, whether or not the address exists. This endpoint needs no
    credential, so a different answer would turn it into a way to test which
    addresses are customers.
    """
    try:
        await members.request_password_reset(
            session, settings=settings, email_provider=sender, email=payload.email
        )
    except EmailDeliveryError:
        # Still 202. Reporting the delivery failure would reveal that the
        # address exists, which is the thing this endpoint must not say.
        return


@router.post(
    "/auth/password/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password from a reset link",
)
async def confirm_reset(payload: PasswordResetConfirm, session: UnscopedSession) -> None:
    """EF-02: single-use, 30 minutes, and every session is revoked."""
    try:
        await members.confirm_password_reset(
            session, token=payload.token, new_password=payload.password
        )
    except members.MemberError as error:
        raise _as_problem(error) from error
