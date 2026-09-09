"""Profile and organization settings (EF-04, EF-05).

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

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import ActorType, AuditLog, Organization, User
from app.uuid7 import uuid7

logger = get_logger(__name__)

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
