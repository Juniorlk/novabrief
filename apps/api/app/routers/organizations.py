"""Profile, organization settings, export and deletion (EF-04, EF-05, EF-06).

Thin over `app.services.organizations`, like the other routers. The one thing
that happens here and nowhere else is `exclude_unset=True`: it is what tells
the service which fields the client actually sent, so an omitted field is left
alone instead of being overwritten with a default.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.config import Settings, get_settings
from app.deps import AdminCaller, CurrentCaller, OwnerCaller, ScopedSession
from app.errors import ProblemError
from app.presenters import current_session, organization_export, organization_profile
from app.routers.members import EmailSender
from app.services import organizations
from schemas.auth import (
    CurrentSession,
    DeletionScheduled,
    OrganizationExport,
    OrganizationProfile,
    UpdateOrganizationRequest,
    UpdateProfileRequest,
)

router = APIRouter(tags=["organizations"])

_STATUS_FOR_CODE = {
    "FIELD_NOT_NULLABLE": 422,
    "RETENTION_INCREASE_REQUIRES_PLAN": 409,
    "DELETION_ALREADY_REQUESTED": 409,
    "NO_DELETION_PENDING": 409,
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


@router.get(
    "/organizations/current/export",
    response_model=OrganizationExport,
    summary="Export everything the organization owns",
)
async def export_organization(
    caller: AdminCaller,
    session: ScopedSession,
    response: Response,
) -> OrganizationExport:
    """EF-06. Admin or Owner.

    Not restricted to the Owner: an administrator can already read the members
    and the settings through this API, so an export gives them no reach they
    did not have. Deletion is a different matter and is Owner-only.

    Served inline as JSON with a filename, so a browser saves it rather than
    rendering it into the address bar.
    """
    bundle = await organizations.export_organization(session, organization=caller.organization)
    document = organization_export(bundle)
    stamp = document.exported_at.strftime("%Y%m%d")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="novabrief-export-{caller.organization.id}-{stamp}.json"'
    )
    return document


@router.delete(
    "/organizations/current",
    response_model=DeletionScheduled,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Schedule the organization's deletion",
)
async def request_deletion(
    caller: OwnerCaller,
    session: ScopedSession,
    sender: EmailSender,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeletionScheduled:
    """EF-06. Owner only, and nothing is destroyed yet.

    202 rather than 204: the request is accepted, the deletion happens later.
    A 204 would tell the client the data is gone, which for the next seven days
    is untrue.
    """
    try:
        schedule = await organizations.request_deletion(
            session,
            settings=settings,
            email_provider=sender,
            organization=caller.organization,
            actor=caller.user,
        )
    except organizations.OrganizationError as error:
        raise _as_problem(error) from error

    return DeletionScheduled(
        deletion_requested_at=schedule.requested_at,
        purge_after=schedule.purge_after,
        retraction_days=organizations.DELETION_RETRACTION.days,
    )


@router.post(
    "/organizations/current/deletion/cancel",
    response_model=OrganizationProfile,
    summary="Cancel a scheduled deletion",
)
async def cancel_deletion(
    caller: OwnerCaller,
    session: ScopedSession,
    sender: EmailSender,
) -> OrganizationProfile:
    """EF-06: the retraction. Owner only, within the seven days."""
    try:
        organization = await organizations.cancel_deletion(
            session,
            email_provider=sender,
            organization=caller.organization,
            actor=caller.user,
        )
    except organizations.OrganizationError as error:
        raise _as_problem(error) from error

    return organization_profile(organization)
