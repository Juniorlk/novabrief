"""Profile and organization settings endpoints (EF-04, EF-05).

Thin over `app.services.organizations`, like the other routers. The one thing
that happens here and nowhere else is `exclude_unset=True`: it is what tells
the service which fields the client actually sent, so an omitted field is left
alone instead of being overwritten with a default.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import AdminCaller, CurrentCaller, ScopedSession
from app.errors import ProblemError
from app.presenters import current_session, organization_profile
from app.services import organizations
from schemas.auth import (
    CurrentSession,
    OrganizationProfile,
    UpdateOrganizationRequest,
    UpdateProfileRequest,
)

router = APIRouter(tags=["organizations"])

_STATUS_FOR_CODE = {
    "FIELD_NOT_NULLABLE": 422,
    "RETENTION_INCREASE_REQUIRES_PLAN": 409,
}


def _as_problem(error: organizations.OrganizationError) -> ProblemError:
    return ProblemError(
        status_code=_STATUS_FOR_CODE.get(error.code, 400),
        code=error.code,
        title=str(error),
    )


@router.patch(
    "/me",
    response_model=CurrentSession,
    summary="Update the signed-in user's profile",
)
async def update_me(
    payload: UpdateProfileRequest,
    caller: CurrentCaller,
    session: ScopedSession,
) -> CurrentSession:
    """EF-04. Any caller, on their own account only.

    There is no `PATCH /users/{id}`: the target is always the token's subject,
    so no request can name someone else.
    """
    try:
        user = await organizations.update_profile(
            session,
            organization=caller.organization,
            user=caller.user,
            changes=payload.model_dump(exclude_unset=True),
        )
    except organizations.OrganizationError as error:
        raise _as_problem(error) from error

    return current_session(user=user, organization=caller.organization)


@router.patch(
    "/organizations/current",
    response_model=OrganizationProfile,
    summary="Update the organization's settings",
)
async def update_current_organization(
    payload: UpdateOrganizationRequest,
    caller: AdminCaller,
    session: ScopedSession,
) -> OrganizationProfile:
    """EF-05. Admins and Owners only; the tenant comes from the token."""
    try:
        organization = await organizations.update_organization(
            session,
            organization=caller.organization,
            actor=caller.user,
            changes=payload.model_dump(exclude_unset=True),
        )
    except organizations.OrganizationError as error:
        raise _as_problem(error) from error

    return organization_profile(organization)
