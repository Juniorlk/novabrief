"""Profile, organization settings, export and deletion (EF-04, EF-05, EF-06).

Both updates are partial: the caller sends only what changes, and anything
absent is left alone. That distinction is carried by the request model's
`model_fields_set`, so the routers pass a mapping built with
`model_dump(exclude_unset=True)` rather than the model's full contents — the
difference between "leave the legal identifier alone" and "clear it" is
otherwise impossible to express.

`None` in that mapping therefore means "set this to nothing", and it is only
accepted for the one column that is nullable in the database. For every other
field a null is a client mistake and is refused, rather than being coerced into
an empty string that would later read as a real value.

Both write to the audit log: EF-05 changes the lifetime of customer data
(retention) and the vocabulary sent to a third party (the lexicon), and section
21.2 wants those traceable. The entry records which fields changed, never their
values — a full name and a legal identifier are personal data and the audit log
is read by operators.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import set_current_organization
from app.email import EmailProvider, Message
from app.logging import get_logger
from app.models import ActorType, AuditLog, Invitation, Organization, User
from app.uuid7 import uuid7

logger = get_logger(__name__)

# EF-06 fixes this at seven days. It is a promise made to the customer, not a
# plan value, so it belongs here rather than in the `plans` table (ADR-09).
DELETION_RETRACTION = timedelta(days=7)

_PROFILE_FIELDS = ("full_name", "locale", "timezone")
_ORGANIZATION_FIELDS = (
    "name",
    "legal_id",
    "default_language",
    "audio_retention_days",
    "lexicon",
)
# The only column either update may set back to nothing: it is nullable in the
# database, and an organization that entered its RCCM by mistake has to be able
# to take it out again.
_CLEARABLE = frozenset({"legal_id"})


class OrganizationError(Exception):
    """An update was refused. Carries a stable code for the client."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _normalize_lexicon(terms: list[str]) -> list[str]:
    """Trim, drop blanks, and remove duplicates while keeping the order.

    Order is kept because an organization writes its most important terms
    first, and the transcription provider caps how many it accepts: dropping
    the wrong end of the list would silently lose the terms that mattered.
    Comparison is case-insensitive — "Novafrik" and "novafrik" are the same
    keyterm — but the spelling the customer chose is what is stored.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for term in terms:
        cleaned = " ".join(term.split())
        if not cleaned:
            continue
        folded = cleaned.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        kept.append(cleaned)
    return kept


def _applied(changes: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Keep the recognised fields, refusing a null where none is allowed."""
    applied: dict[str, Any] = {}
    for field in fields:
        if field not in changes:
            continue
        value = changes[field]
        if value is None and field not in _CLEARABLE:
            raise OrganizationError("FIELD_NOT_NULLABLE", f"{field} cannot be set to null")
        applied[field] = value
    return applied


async def update_profile(
    session: AsyncSession,
    *,
    organization: Organization,
    user: User,
    changes: Mapping[str, Any],
) -> User:
    """EF-04: a user edits their own name, language and timezone.

    Role is not among the fields on purpose. It is not merely absent from the
    request model — accepting it here would let any member promote themselves
    to Owner with a single call, which is the whole of EF-03's access control
    undone in one line.

    The session must already be scoped to `organization`.
    """
    applied = _applied(changes, _PROFILE_FIELDS)
    if not applied:
        # Nothing to do, and nothing to audit: an empty body is a no-op rather
        # than an error, so a client that diffs a form and finds no change does
        # not have to special-case it.
        return user

    if "full_name" in applied:
        applied["full_name"] = applied["full_name"].strip()
        if not applied["full_name"]:
            raise OrganizationError("FIELD_NOT_NULLABLE", "full_name cannot be blank")

    for field, value in applied.items():
        setattr(user, field, value)

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=user.id,
            actor_type=ActorType.USER.value,
            action="profile.updated",
            target_type="user",
            target_id=user.id,
            metadata_json={"fields": sorted(applied)},
        )
    )
    logger.info("profile_updated", fields=sorted(applied))
    return user


async def update_organization(
    session: AsyncSession,
    *,
    organization: Organization,
    actor: User,
    changes: Mapping[str, Any],
) -> Organization:
    """EF-05: an Admin or Owner edits the organization's settings.

    Plan, quota and status are not accepted. They are billing state written by
    the payment flow from the `plans` table (ADR-09); letting a customer set
    them here would be a free upgrade.

    Retention may only be lowered. Raising it means keeping audio longer than
    the plan bought, and the plan ceiling lives in a table that does not exist
    until the billing lot — writing the limit here would put a plan value in
    application code, which ADR-09 forbids. Lowering is always safe: it deletes
    sooner, never later, so it is allowed now and raising waits for the plans
    table.

    The session must already be scoped to `organization`.
    """
    applied = _applied(changes, _ORGANIZATION_FIELDS)
    if not applied:
        return organization

    if "name" in applied:
        applied["name"] = applied["name"].strip()
        if not applied["name"]:
            raise OrganizationError("FIELD_NOT_NULLABLE", "name cannot be blank")

    if "legal_id" in applied and applied["legal_id"] is not None:
        # An empty string is how a web form sends a cleared input; store the
        # absence rather than a blank that would print as a real identifier.
        applied["legal_id"] = applied["legal_id"].strip() or None

    if "audio_retention_days" in applied:
        requested = applied["audio_retention_days"]
        if requested > organization.audio_retention_days:
            raise OrganizationError(
                "RETENTION_INCREASE_REQUIRES_PLAN",
                "audio retention can only be reduced; raising it depends on the plan",
            )

    if "lexicon" in applied:
        applied["lexicon"] = _normalize_lexicon(applied["lexicon"])

    for field, value in applied.items():
        setattr(organization, field, value)

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=actor.id,
            actor_type=ActorType.USER.value,
            action="organization.updated",
            target_type="organization",
            target_id=organization.id,
            metadata_json={"fields": sorted(applied)},
        )
    )
    logger.info("organization_updated", fields=sorted(applied))
    return organization


# --------------------------------------------------------------------------
# EF-06: export and deletion
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeletionSchedule:
    """When a deletion was asked for, and when it becomes irreversible."""

    requested_at: datetime
    purge_after: datetime


@dataclass(frozen=True)
class ExportBundle:
    """Every row an organization owns, gathered but not yet formatted.

    Rows rather than a response model: the service decides *what* leaves the
    database, `app.presenters` decides how it looks. Keeping the split means
    the rule that matters here — no credential ever enters an export — is
    stated where the rows are read.
    """

    organization: Organization
    members: Sequence[User]
    invitations: Sequence[Invitation]
    audit_entries: Sequence[AuditLog]


async def export_organization(session: AsyncSession, *, organization: Organization) -> ExportBundle:
    """EF-06: gather everything the organization owns.

    No filter is written here. The session is scoped, so RLS is what confines
    the reads to one tenant — the same mechanism as everywhere else, rather
    than a `where` clause that could be forgotten.

    Meetings, reports and the audio manifest join this when those tables exist
    (lot L2). The audio itself will be listed as presigned URLs rather than
    embedded: a JSON document holding hours of Opus is not a document anyone
    can open.
    """
    members = (await session.scalars(select(User).order_by(User.created_at))).all()
    invitations = (await session.scalars(select(Invitation).order_by(Invitation.created_at))).all()
    audit_entries = (await session.scalars(select(AuditLog).order_by(AuditLog.created_at))).all()

    logger.info(
        "organization_exported",
        members=len(members),
        invitations=len(invitations),
        audit_entries=len(audit_entries),
    )
    return ExportBundle(
        organization=organization,
        members=members,
        invitations=invitations,
        audit_entries=audit_entries,
    )


async def request_deletion(
    session: AsyncSession,
    *,
    settings: Settings,
    email_provider: EmailProvider,
    organization: Organization,
    actor: User,
) -> DeletionSchedule:
    """EF-06: schedule the organization's deletion, seven days out.

    Nothing is destroyed here. The row is marked and the clock starts, which is
    what makes the promise keepable: an Owner who acted in anger, or whose
    account was taken over, has a week to undo it. The purge itself is
    :func:`purge_due_organizations`.
    """
    if organization.deletion_requested_at is not None:
        # Re-requesting would silently restart the countdown, which is the
        # opposite of what a retraction window is for.
        raise OrganizationError(
            "DELETION_ALREADY_REQUESTED",
            "this organization is already scheduled for deletion",
        )

    requested_at = datetime.now(UTC)
    organization.deletion_requested_at = requested_at
    purge_after = requested_at + DELETION_RETRACTION

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=actor.id,
            actor_type=ActorType.USER.value,
            action="organization.deletion_requested",
            target_type="organization",
            target_id=organization.id,
            metadata_json={"purge_after": purge_after.isoformat()},
        )
    )

    if actor.email:
        # The Owner is told even though they are the one who asked: if they did
        # not ask, this message is how they find out in time to stop it.
        await email_provider.send(
            Message(
                to=actor.email,
                subject="NovaBrief - suppression de votre organisation programmee",
                text=(
                    f"La suppression de l'organisation {organization.name} a ete demandee.\n\n"
                    f"Toutes les donnees seront definitivement effacees le "
                    f"{purge_after:%d/%m/%Y a %Hh%M UTC}.\n\n"
                    f"Si vous n'etes pas a l'origine de cette demande, annulez-la "
                    f"immediatement depuis {settings.web_base_url}/settings et changez "
                    f"votre mot de passe.\n\n"
                    f"Vous pouvez exporter vos donnees tant que le delai court."
                ),
            )
        )

    logger.info("organization_deletion_requested", purge_after=purge_after.isoformat())
    return DeletionSchedule(requested_at=requested_at, purge_after=purge_after)


async def cancel_deletion(
    session: AsyncSession,
    *,
    email_provider: EmailProvider,
    organization: Organization,
    actor: User,
) -> Organization:
    """EF-06: change your mind, within the seven days."""
    if organization.deletion_requested_at is None:
        raise OrganizationError(
            "NO_DELETION_PENDING",
            "no deletion is scheduled for this organization",
        )

    organization.deletion_requested_at = None

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=actor.id,
            actor_type=ActorType.USER.value,
            action="organization.deletion_cancelled",
            target_type="organization",
            target_id=organization.id,
        )
    )

    if actor.email:
        await email_provider.send(
            Message(
                to=actor.email,
                subject="NovaBrief - suppression annulee",
                text=(
                    f"La suppression de l'organisation {organization.name} a ete annulee. "
                    f"Vos donnees sont conservees et rien n'a ete efface."
                ),
            )
        )

    logger.info("organization_deletion_cancelled")
    return organization


async def purge_due_organizations(
    session: AsyncSession, *, now: datetime | None = None
) -> list[uuid.UUID]:
    """EF-06: erase the organizations whose retraction window has expired.

    Two steps, and the split is the point. Finding them crosses tenants, which
    RLS forbids, so it goes through `organizations_due_for_purge` — a read-only
    SECURITY DEFINER function. Deleting each one then happens inside a session
    scoped to that organization, through the ordinary policy: the single
    irreversible write in the system is not handed a way around RLS.

    The six tenant tables cascade from `organizations`, so removing that row
    removes the members, devices, tokens, invitations, resets and audit trail
    with it. That is what EF-06's acceptance criterion asks for: nothing left
    but encrypted backups, themselves purged within thirty days.

    The caller passes an unscoped session and commits.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - DELETION_RETRACTION

    due = (
        await session.execute(
            text("SELECT id FROM organizations_due_for_purge(:before)"), {"before": cutoff}
        )
    ).all()

    purged: list[uuid.UUID] = []
    for row in due:
        await set_current_organization(session, row.id)
        # A Core delete, not `session.delete`: the ORM would load the members
        # and try to null their `organization_id` rather than let the database
        # cascade run. The cascade is the whole mechanism here, so nothing
        # should come between it and the row.
        result = cast(
            "CursorResult[Any]",
            await session.execute(delete(Organization).where(Organization.id == row.id)),
        )
        if result.rowcount == 0:  # pragma: no cover - only under a concurrent run
            continue
        purged.append(row.id)
        # No audit entry: it lives in `audit_log`, which the cascade has just
        # removed. The operational record is this log line and the backups.
        logger.info("organization_purged", purged_organization_id=str(row.id))

    return purged
