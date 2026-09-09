"""ORM models for lot L1: organizations, users, devices and the audit log.

Every table holding customer data carries a denormalised `organization_id` and
a Row-Level Security policy keyed on it (ADR-04, section 19.2). Denormalised on
purpose: RLS evaluates one predicate per row, and a policy that has to join to
find the tenant is both slower and easier to get wrong.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.uuid7 import uuid7


class Role(StrEnum):
    """What a member may do inside their organization."""

    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class OrganizationStatus(StrEnum):
    """Where an organization sits in the billing cycle (section 20.3)."""

    ACTIVE = "ACTIVE"
    GRACE = "GRACE"
    HOLD = "HOLD"
    FREE_FALLBACK = "FREE_FALLBACK"


class ActorType(StrEnum):
    """Who performed an audited action."""

    USER = "USER"
    SYSTEM = "SYSTEM"
    NOVAFRIK = "NOVAFRIK"


def _pk() -> Mapped[uuid.UUID]:
    """Primary key column: UUID v7, sortable by creation time (section 19.2)."""
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


class Organization(Base):
    """A tenant. Everything else in the schema hangs off this row."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_id: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(2), nullable=False, default="CM")

    # Plan, quota and retention are set from the `plans` table (ADR-09); they
    # are stored here as the organization's current state, never as literals in
    # application code.
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    cycle_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cycle_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    consumed_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    audio_retention_days: Mapped[int] = mapped_column(nullable=False, default=30)

    default_language: Mapped[str] = mapped_column(String(5), nullable=False, default="fr")
    # Proper nouns and acronyms passed to the transcription provider as
    # keyterms (EF-05), which is what makes local names come back spelled right.
    lexicon: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=OrganizationStatus.ACTIVE.value
    )
    momo_number: Mapped[str | None] = mapped_column(String(20))

    # EF-06: deletion has a seven-day grace period, so the row is marked rather
    # than removed and a customer can change their mind.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # passive_deletes: without it SQLAlchemy loads the members on a delete and
    # issues `UPDATE users SET organization_id = NULL`, which detaches them
    # instead of removing them — and under RLS fails outright. The database
    # already cascades (EF-06 depends on it), so the ORM must stand aside.
    users: Mapped[list[User]] = relationship(back_populates="organization", passive_deletes=True)

    __table_args__ = (
        CheckConstraint("consumed_seconds >= 0", name="organizations_consumed_non_negative"),
        CheckConstraint("audio_retention_days > 0", name="organizations_retention_positive"),
    )


class User(Base):
    """A person. Belongs to exactly one organization in V1."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # EF-01 allows signing up with either an email or an E.164 phone number, so
    # neither is individually mandatory; the check constraint enforces that at
    # least one exists.
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(20))

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=Role.MEMBER.value)

    locale: Mapped[str] = mapped_column(String(5), nullable=False, default="fr")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Africa/Douala")

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_secret: Mapped[str | None] = mapped_column(String(64))

    # EF-03: a revoked member loses access in under a minute. Revocation is
    # recorded here and checked on every token refresh.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="users")

    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name="users_email_or_phone_required",
        ),
        # Uniqueness is global rather than per organization: an address
        # identifies a person at login, before any organization is known.
        UniqueConstraint("email", name="users_email_unique"),
        UniqueConstraint("phone", name="users_phone_unique"),
        Index("users_organization_idx", "organization_id"),
    )


class Device(Base):
    """A Windows machine linked to a user (section 19.1, EF-11)."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    os_version: Mapped[str | None] = mapped_column(String(64))
    app_version: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("devices_user_idx", "user_id"),)


class RefreshToken(Base):
    """An issued refresh token, stored hashed.

    Refresh tokens rotate: using one issues a replacement and marks the old one
    used. A second use of an already-used token means it leaked, and the whole
    family is revoked rather than just that token (section 17.3).

    Only the hash is stored. A database dump must not hand an attacker working
    credentials.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL")
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Every token descended from one login shares a family id, so detecting a
    # replay lets us revoke that whole login rather than every session the user
    # has open.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("token_hash", name="refresh_tokens_hash_unique"),
        Index("refresh_tokens_family_idx", "family_id"),
        Index("refresh_tokens_user_idx", "user_id"),
    )


class Invitation(Base):
    """A pending invitation to join an organization (EF-03)."""

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=Role.MEMBER.value)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("token_hash", name="invitations_token_unique"),
        Index("invitations_org_email_idx", "organization_id", "email"),
    )


class AuditLog(Base):
    """Who did what, when (section 19.1, section 21.2).

    Written for anything that changes access, money or data lifetime. Never
    for reads of ordinary content, which would bury the entries that matter.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ActorType.USER.value
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    ip: Mapped[str | None] = mapped_column(INET)
    # Free-form context. It records why an action happened, never what a
    # meeting contained.
    reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("audit_log_org_created_idx", "organization_id", "created_at"),)


class PasswordReset(Base):
    """A single-use password reset link, valid 30 minutes (EF-02)."""

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("token_hash", name="password_resets_token_unique"),)


class EmailVerification(Base):
    """A single-use link proving an address belongs to whoever signed up (EF-02)."""

    __tablename__ = "email_verifications"

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # The address the link proves, stored beside the user rather than read from
    # it: when changing an address becomes possible, a link issued for the old
    # one must not confirm the new one.
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("token_hash", name="email_verifications_token_unique"),)


# Tables that hold customer data and therefore need an RLS policy. The list is
# used by the migration and asserted by a test, so a new table cannot be added
# without either a policy or a deliberate exemption.
TENANT_TABLES: tuple[str, ...] = (
    "users",
    "devices",
    "refresh_tokens",
    "invitations",
    "audit_log",
    "password_resets",
    "email_verifications",
)

# `organizations` is the tenant itself: its policy compares `id`, not
# `organization_id`, so it is handled separately rather than bent into the loop.
ORGANIZATION_TABLE = "organizations"


def is_tenant_scoped(table_name: str) -> bool:
    """Whether a table must carry the standard organization policy."""
    return table_name in TENANT_TABLES


__all__ = [
    "ORGANIZATION_TABLE",
    "TENANT_TABLES",
    "ActorType",
    "AuditLog",
    "Device",
    "EmailVerification",
    "Invitation",
    "Organization",
    "OrganizationStatus",
    "PasswordReset",
    "RefreshToken",
    "Role",
    "User",
    "is_tenant_scoped",
]
