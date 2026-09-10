"""FastAPI dependencies: database sessions and the authenticated caller.

Two kinds of session, and the distinction matters:

* :func:`unscoped_session` — no tenant is set, so RLS shows nothing. Only
  sign-in and registration use it, because until the caller is identified there
  is no organization to scope to.
* :func:`scoped_session` — `app.current_org_id` is set from the caller's token
  before anything is read or written, which is the ADR-04 mechanism.

Any endpoint touching customer data takes the scoped one. Taking the unscoped
session by mistake yields empty results rather than a leak, which is the
failure mode we want, but the names make the choice deliberate rather than
accidental.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import set_current_organization
from app.errors import ProblemError
from app.logging import get_logger
from app.models import Organization, Role, User
from app.security import AccessClaims, TokenError, decode_access_token

logger = get_logger(__name__)


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:  # pragma: no cover - only if the app was built wrongly
        raise ProblemError(
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            title="The database is not configured.",
        )
    return factory


async def unscoped_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A transaction with no tenant set.

    Reserved for sign-in and registration; every other endpoint uses
    :func:`scoped_session`.
    """
    factory = _session_factory(request)
    async with factory() as session, session.begin():
        yield session


# Declared so the generated OpenAPI document says these endpoints need a
# bearer token, which is what puts the Authorize button in the interactive
# documentation. Without it every authenticated endpoint answers 401 when tried
# from a browser and looks broken rather than protected.
#
# `auto_error=False` on purpose: FastAPI's own 403 would bypass our Problem
# Details format (ADR-07), and a client parsing error codes would meet a shape
# it has never seen on exactly the path it hits most often.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="The access token returned by POST /auth/token or /auth/register.",
)


def access_claims(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> AccessClaims:
    """Verify the bearer token and return its claims.

    The token is read here once and reused by everything downstream, so a
    request never verifies the same signature twice.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ProblemError(
            status_code=401,
            code="UNAUTHENTICATED",
            title="This endpoint requires an access token.",
        )
    try:
        return decode_access_token(settings=settings, token=credentials.credentials.strip())
    except TokenError as exc:
        raise ProblemError(
            status_code=401,
            code="INVALID_TOKEN",
            title="The access token is not valid.",
            detail=str(exc),
        ) from exc


def after_commit(request: Request, callback: Callable[[], None]) -> None:
    """Do this once the request's transaction has actually committed.

    Queueing a background task from inside an open transaction is a race the
    workers already guard against and the API did not: the message reaches
    Redis immediately, a worker can pick it up in single-digit milliseconds,
    and it reads a row that still holds the *previous* state. The worker then
    does the right thing — it declines to act on a meeting that is not where it
    expects — and the meeting is never processed at all. No error anywhere; it
    simply sits in QUEUED for ever.

    So the dispatch waits here, and the session dependency below runs it after
    the commit.
    """
    pending: list[Callable[[], None]] = getattr(request.state, "after_commit", [])
    pending.append(callback)
    request.state.after_commit = pending


async def scoped_session(
    request: Request,
    claims: Annotated[AccessClaims, Depends(access_claims)],
) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to the caller's organization.

    The scope comes from the token's signed `org` claim, never from a path or
    body parameter: anything the client can choose per request would let a
    caller ask for another tenant's rows.

    Whatever :func:`after_commit` queued runs once this transaction has
    committed, and only then. If the handler raised, the `async with` rolls
    back and re-raises before reaching that line, so nothing queued by a failed
    request is ever dispatched.
    """
    factory = _session_factory(request)
    request.state.after_commit = []
    async with factory() as session, session.begin():
        await set_current_organization(session, claims.organization_id)
        yield session

    for callback in request.state.after_commit:
        try:
            callback()
        except Exception:
            # The response has already been decided and the database already
            # says the work is queued, so failing the request now would be a
            # lie in the other direction. Loud in the logs instead: a meeting
            # whose message never reached the broker stays in QUEUED and needs
            # a human or the retry button.
            logger.error("after_commit_failed", exc_info=True)


@dataclass(frozen=True)
class Caller:
    """The authenticated user and their organization."""

    user: User
    organization: Organization

    @property
    def is_admin(self) -> bool:
        """Whether this caller may administer the organization."""
        return self.user.role in {Role.OWNER.value, Role.ADMIN.value}

    @property
    def is_owner(self) -> bool:
        """Whether this caller owns the organization."""
        return self.user.role == Role.OWNER.value


async def current_caller(
    session: Annotated[AsyncSession, Depends(scoped_session)],
    claims: Annotated[AccessClaims, Depends(access_claims)],
) -> Caller:
    """Resolve the caller, or refuse the request.

    The user is loaded through the scoped session, so a token naming a user in
    another organization finds nothing. The signed claim and the row have to
    agree, and RLS is what makes them agree — not a comparison we could forget
    to write.
    """
    user = await session.get(User, claims.user_id)
    if user is None or user.revoked_at is not None:
        # EF-03: a revoked member is refused immediately, without waiting for
        # the token to expire.
        raise ProblemError(
            status_code=401,
            code="UNAUTHENTICATED",
            title="This account is no longer active.",
        )

    organization = await session.get(Organization, user.organization_id)
    if organization is None:  # pragma: no cover - foreign key makes this unreachable
        raise ProblemError(
            status_code=401,
            code="UNAUTHENTICATED",
            title="This account is no longer active.",
        )

    return Caller(user=user, organization=organization)


async def require_admin(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    """Refuse callers who may not administer the organization."""
    if not caller.is_admin:
        raise ProblemError(
            status_code=403,
            code="FORBIDDEN",
            title="This action requires an Admin or Owner role.",
        )
    return caller


async def require_owner(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    """Refuse callers who do not own the organization."""
    if not caller.is_owner:
        raise ProblemError(
            status_code=403,
            code="FORBIDDEN",
            title="This action requires the Owner role.",
        )
    return caller


CurrentCaller = Annotated[Caller, Depends(current_caller)]
AdminCaller = Annotated[Caller, Depends(require_admin)]
OwnerCaller = Annotated[Caller, Depends(require_owner)]
ScopedSession = Annotated[AsyncSession, Depends(scoped_session)]
UnscopedSession = Annotated[AsyncSession, Depends(unscoped_session)]
