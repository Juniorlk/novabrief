"""Request and response shapes for accounts and organizations.

These Pydantic models are the source of truth for the contract (ADR-10): the
TypeScript types the web app and the desktop client use are generated from
them, so the three cannot drift apart.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

# EF-01: phone numbers are stored in E.164, which is unambiguous across the
# countries NovaBrief will open in and is what Mobile Money expects.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

MIN_PASSWORD_LENGTH = 10

Role = Literal["OWNER", "ADMIN", "MEMBER"]
Locale = Literal["fr", "en"]

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)]

# One entry of an organization's lexicon: a proper noun or an acronym, not a
# sentence.
LexiconTerm = Annotated[str, Field(min_length=1, max_length=80)]


def _known_timezone(value: str) -> str:
    """Reject anything the IANA database does not know.

    A timezone is not decoration: renewal reminders go out at 08:00 in the
    user's zone (section 20.3) and retention is counted in local days. An
    unchecked string would be accepted here and blow up months later inside a
    scheduled job, far from whoever typed it.
    """
    candidate = value.strip()
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        message = f"unknown timezone: {candidate!r}"
        raise ValueError(message) from exc
    return candidate


# `str` rather than a Literal of every zone: the IANA list changes twice a year
# and freezing it here would mean a release to accept a new one.
Timezone = Annotated[str, Field(max_length=64), AfterValidator(_known_timezone)]


class _Base(BaseModel):
    """Shared configuration: reject unknown fields."""

    # A typo in a client payload should be a visible 422, not a value silently
    # ignored while the caller believes it was applied.
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_Base):
    """EF-01: create an account and its organization in one step."""

    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=1, max_length=200)
    password: Password

    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)

    locale: Locale = "fr"
    timezone: Timezone = "Africa/Douala"

    @field_validator("phone")
    @classmethod
    def _phone_is_e164(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().replace(" ", "")
        if not _E164.match(candidate):
            message = "phone must be in E.164 format, for example +237690000000"
            raise ValueError(message)
        return candidate

    @model_validator(mode="after")
    def _needs_one_identifier(self) -> RegisterRequest:
        if self.email is None and self.phone is None:
            message = "either email or phone is required"
            raise ValueError(message)
        return self


class LoginRequest(_Base):
    """Sign in with whichever identifier the account was created with."""

    password: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _needs_one_identifier(self) -> LoginRequest:
        if self.email is None and self.phone is None:
            message = "either email or phone is required"
            raise ValueError(message)
        return self


class RefreshRequest(_Base):
    """Exchange a refresh token for a new pair."""

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPair(_Base):
    """What a successful authentication returns.

    The refresh token is opaque and single-use: presenting it returns a new
    pair, and presenting it a second time revokes the whole family.
    """

    access_token: str
    refresh_token: str
    # Not a secret: this is the OAuth 2.0 token *type*, which the client
    # echoes in the Authorization header.
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserProfile(_Base):
    """EF-04: the profile the web and desktop apps display."""

    id: uuid.UUID
    email: EmailStr | None
    phone: str | None
    full_name: str
    role: Role
    locale: Locale
    timezone: str
    email_verified: bool
    created_at: datetime


class OrganizationProfile(_Base):
    """EF-05: organization settings."""

    id: uuid.UUID
    name: str
    legal_id: str | None
    market: str
    plan_code: str
    default_language: str
    audio_retention_days: int
    # Null until a plan is assigned (lot L5): "no quota configured" is not the
    # same fact as "a quota of zero", and a client showing 0 h remaining to an
    # organization that simply has no plan yet would be lying to it.
    quota_seconds: int | None
    consumed_seconds: int
    status: str
    # Proper nouns and acronyms handed to the transcription provider as
    # keyterms, which is what makes local names come back spelled correctly.
    lexicon: list[str]
    # EF-06: set while a deletion is pending, null otherwise. The client needs
    # it to show the banner and the countdown, so it belongs in the profile
    # rather than behind a second call.
    deletion_requested_at: datetime | None = None
    created_at: datetime


class CurrentSession(_Base):
    """`GET /me`: who is signed in, and where."""

    user: UserProfile
    organization: OrganizationProfile


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "AcceptInvitationRequest",
    "CurrentSession",
    "DeletionScheduled",
    "ExportedAuditEntry",
    "ExportedInvitation",
    "ExportedMember",
    "InvitationCreated",
    "InviteMemberRequest",
    "LoginRequest",
    "MemberSummary",
    "OrganizationExport",
    "OrganizationProfile",
    "PasswordResetConfirm",
    "PasswordResetRequest",
    "RefreshRequest",
    "RegisterRequest",
    "Role",
    "Timezone",
    "TokenPair",
    "UpdateOrganizationRequest",
    "UpdateProfileRequest",
    "UserProfile",
    "VerifyEmailRequest",
]


class InviteMemberRequest(_Base):
    """EF-03: invite someone into the organization, with a role."""

    email: EmailStr
    # Ownership is not transferable by invitation: an organization has one
    # Owner and changing it is a separate, audited act.
    role: Literal["ADMIN", "MEMBER"] = "MEMBER"


class InvitationCreated(_Base):
    """What the inviter gets back. Never the token: that went to the invitee."""

    id: uuid.UUID
    email: EmailStr
    role: Literal["ADMIN", "MEMBER"]
    expires_at: datetime


class AcceptInvitationRequest(_Base):
    """EF-03: accept in one click, which also creates the account."""

    token: str = Field(min_length=1, max_length=512)
    full_name: str = Field(min_length=1, max_length=200)
    password: Password
    locale: Locale = "fr"
    timezone: Timezone = "Africa/Douala"


class MemberSummary(_Base):
    """A member as listed to an administrator."""

    id: uuid.UUID
    email: EmailStr | None
    full_name: str
    role: Role
    revoked: bool
    created_at: datetime


class VerifyEmailRequest(_Base):
    """EF-02: confirm an address from the link that was emailed to it."""

    token: str = Field(min_length=1, max_length=512)


class PasswordResetRequest(_Base):
    """EF-02: ask for a reset link."""

    email: EmailStr


class PasswordResetConfirm(_Base):
    """EF-02: consume the link and choose a new password."""

    token: str = Field(min_length=1, max_length=512)
    password: Password


class UpdateProfileRequest(_Base):
    """EF-04: what a user may change about themselves.

    Every field is optional: a client sends only what it is changing, and
    omitting a field leaves it alone. Role is absent on purpose — nobody
    promotes themselves.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    locale: Locale | None = None
    timezone: Timezone | None = None


class UpdateOrganizationRequest(_Base):
    """EF-05: organization settings an administrator may change.

    Plan, quota and status are absent: they are billing state, changed by the
    payment flow and never by a customer request (ADR-09).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    # RCCM or NIU in Cameroon; free text because the format differs per market.
    legal_id: str | None = Field(default=None, max_length=64)
    default_language: Locale | None = None
    # Reducing is always allowed; raising it is a plan matter (see the service).
    audio_retention_days: int | None = Field(default=None, ge=1, le=3650)
    # Proper nouns and acronyms handed to the transcription provider as
    # keyterms, which is what makes local names come back spelled correctly.
    # Bounded on both axes: the provider charges for the list and refuses an
    # oversized one, so a paste of a whole document has to fail here.
    lexicon: list[LexiconTerm] | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------
# EF-06: export and deletion
# --------------------------------------------------------------------------


class ExportedMember(_Base):
    """One member inside an export.

    Deliberately not a `MemberSummary`: an export is a durable file the
    customer keeps, so it carries the settings that would be needed to
    recreate the account. It carries no password hash, no TOTP secret and no
    token — an export is handed over, and a credential in it would outlive
    every revocation.
    """

    id: uuid.UUID
    email: EmailStr | None
    phone: str | None
    full_name: str
    role: Role
    locale: str
    timezone: str
    email_verified: bool
    revoked: bool
    created_at: datetime


class ExportedInvitation(_Base):
    """One invitation inside an export. The token itself is never included."""

    id: uuid.UUID
    email: EmailStr
    role: Role
    invited_by: uuid.UUID | None
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ExportedAuditEntry(_Base):
    """One audit entry inside an export (section 21.2)."""

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_type: str
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    ip: str | None
    reason: str | None
    metadata: dict[str, Any] | None
    created_at: datetime


class OrganizationExport(_Base):
    """EF-06: everything the organization owns, in one document.

    `format` is a version string rather than a number so a reader can tell at
    a glance what it is holding. Meetings, reports and the audio manifest join
    this document when those tables exist (lot L2), which is why the version
    is stated in the file rather than assumed by whoever opens it.
    """

    format: Literal["novabrief.export.v1"] = "novabrief.export.v1"
    exported_at: datetime
    organization: OrganizationProfile
    members: list[ExportedMember]
    invitations: list[ExportedInvitation]
    audit_log: list[ExportedAuditEntry]


class DeletionScheduled(_Base):
    """EF-06: the answer to a deletion request.

    Both instants are returned because the difference is the whole point: the
    request is recorded now, the data disappears later, and until then the
    customer can change their mind.
    """

    deletion_requested_at: datetime
    purge_after: datetime
    retraction_days: int
