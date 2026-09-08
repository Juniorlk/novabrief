"""Account and session endpoints (EF-01, EF-02, section 17.1).

The routes stay thin: they translate HTTP into a service call and a service
error into a Problem Details code. The rules live in `app.services.auth` so
they are stated once and testable without a client.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.config import Settings, get_settings
from app.deps import CurrentCaller, UnscopedSession
from app.errors import ProblemError
from app.services import auth
from schemas.auth import (
    CurrentSession,
    LoginRequest,
    OrganizationProfile,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserProfile,
)

router = APIRouter(tags=["auth"])

# Service error codes mapped to HTTP status. Anything unmapped becomes a 401
# rather than leaking through as a 500: an authentication path should fail
# closed.
_STATUS_FOR_CODE = {
    "ACCOUNT_EXISTS": status.HTTP_409_CONFLICT,
    "INVALID_CREDENTIALS": status.HTTP_401_UNAUTHORIZED,
    "ACCOUNT_REVOKED": status.HTTP_401_UNAUTHORIZED,
    "INVALID_REFRESH_TOKEN": status.HTTP_401_UNAUTHORIZED,
    "REFRESH_TOKEN_EXPIRED": status.HTTP_401_UNAUTHORIZED,
    "REFRESH_TOKEN_REUSED": status.HTTP_401_UNAUTHORIZED,
}


def _as_problem(error: auth.AuthError) -> ProblemError:
    """Turn a service error into the API's error shape."""
    return ProblemError(
        status_code=_STATUS_FOR_CODE.get(error.code, status.HTTP_401_UNAUTHORIZED),
        code=error.code,
        title=str(error),
    )


def _tokens(issued: auth.IssuedSession) -> TokenPair:
    return TokenPair(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
    )


@router.post(
    "/auth/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its organization",
)
async def register(
    payload: RegisterRequest,
    session: UnscopedSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPair:
    """EF-01: one step, and the creator becomes Owner."""
    try:
        issued = await auth.register(
            session,
            settings=settings,
            full_name=payload.full_name,
            organization_name=payload.organization_name,
            password=payload.password,
            email=payload.email,
            phone=payload.phone,
            locale=payload.locale,
            timezone=payload.timezone,
        )
    except auth.AuthError as error:
        raise _as_problem(error) from error
    return _tokens(issued)


@router.post("/auth/token", response_model=TokenPair, summary="Sign in")
async def token(
    payload: LoginRequest,
    session: UnscopedSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPair:
    try:
        issued = await auth.authenticate(
            session,
            settings=settings,
            password=payload.password,
            email=payload.email,
            phone=payload.phone,
        )
    except auth.AuthError as error:
        raise _as_problem(error) from error
    return _tokens(issued)


@router.post("/auth/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(
    payload: RefreshRequest,
    session: UnscopedSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPair:
    """Presenting a token consumes it; presenting it twice revokes the login."""
    try:
        issued = await auth.refresh(
            session, settings=settings, presented_token=payload.refresh_token
        )
    except auth.AuthError as error:
        raise _as_problem(error) from error
    return _tokens(issued)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def logout(payload: RefreshRequest, session: UnscopedSession) -> None:
    """Signing out revokes the whole family this token belongs to.

    Answers 204 whether or not the token was known: a different answer would
    tell a caller whether a token they hold is still live.
    """
    await auth.revoke_session(session, presented_token=payload.refresh_token)


@router.get("/me", response_model=CurrentSession, summary="The signed-in user")
async def me(caller: CurrentCaller) -> CurrentSession:
    """EF-04 and EF-05: the profile and the organization behind it."""
    return CurrentSession(
        user=UserProfile(
            id=caller.user.id,
            email=caller.user.email,
            phone=caller.user.phone,
            full_name=caller.user.full_name,
            role=caller.user.role,  # type: ignore[arg-type]
            locale=caller.user.locale,  # type: ignore[arg-type]
            timezone=caller.user.timezone,
            email_verified=caller.user.email_verified_at is not None,
            created_at=caller.user.created_at,
        ),
        organization=OrganizationProfile(
            id=caller.organization.id,
            name=caller.organization.name,
            legal_id=caller.organization.legal_id,
            market=caller.organization.market,
            plan_code=caller.organization.plan_code,
            default_language=caller.organization.default_language,
            audio_retention_days=caller.organization.audio_retention_days,
            quota_seconds=caller.organization.quota_seconds,
            consumed_seconds=caller.organization.consumed_seconds,
            status=caller.organization.status,
            lexicon=caller.organization.lexicon,
            created_at=caller.organization.created_at,
        ),
    )
