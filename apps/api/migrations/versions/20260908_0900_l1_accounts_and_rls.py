"""L1: accounts, organizations, and Row-Level Security on every tenant table.

Revision ID: 20260908_0900
Revises:
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0900"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables whose rows belong to one organization, identified by `organization_id`.
TENANT_TABLES = (
    "users",
    "devices",
    "refresh_tokens",
    "invitations",
    "audit_log",
    "password_resets",
)

# The session variable the policies read. `NULLIF(..., '')` is what makes an
# unset variable mean "no rows" instead of raising: a query that forgot to
# scope itself returns nothing, which is a bug that fails safe.
_CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("legal_id", sa.String(64)),
        sa.Column("market", sa.String(2), nullable=False, server_default="CM"),
        sa.Column("plan_code", sa.String(32), nullable=False, server_default="free"),
        sa.Column("cycle_start", sa.DateTime(timezone=True)),
        sa.Column("cycle_end", sa.DateTime(timezone=True)),
        sa.Column("quota_seconds", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consumed_seconds", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("audio_retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("default_language", sa.String(5), nullable=False, server_default="fr"),
        sa.Column(
            "lexicon",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("momo_number", sa.String(20)),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("consumed_seconds >= 0", name="organizations_consumed_non_negative"),
        sa.CheckConstraint("audio_retention_days > 0", name="organizations_retention_positive"),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(20)),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="MEMBER"),
        sa.Column("locale", sa.String(5), nullable=False, server_default="fr"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Douala"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("totp_secret", sa.String(64)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL", name="users_email_or_phone_required"
        ),
        sa.UniqueConstraint("email", name="users_email_unique"),
        sa.UniqueConstraint("phone", name="users_phone_unique"),
    )
    op.create_index("users_organization_idx", "users", ["organization_id"])

    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("os_version", sa.String(64)),
        sa.Column("app_version", sa.String(32)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("devices_user_idx", "devices", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(64)),
        sa.UniqueConstraint("token_hash", name="refresh_tokens_hash_unique"),
    )
    op.create_index("refresh_tokens_family_idx", "refresh_tokens", ["family_id"])
    op.create_index("refresh_tokens_user_idx", "refresh_tokens", ["user_id"])

    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="MEMBER"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token_hash", name="invitations_token_unique"),
    )
    op.create_index("invitations_org_email_idx", "invitations", ["organization_id", "email"])

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_type", sa.String(16), nullable=False, server_default="USER"),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64)),
        sa.Column("target_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ip", postgresql.INET()),
        sa.Column("reason", sa.Text()),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("audit_log_org_created_idx", "audit_log", ["organization_id", "created_at"])

    op.create_table(
        "password_resets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token_hash", name="password_resets_token_unique"),
    )

    _enable_row_level_security()


def _enable_row_level_security() -> None:
    """Turn on RLS and install the isolation policies (ADR-04).

    FORCE matters as much as ENABLE. Without it PostgreSQL exempts the table
    owner from its own policies, and the application role is usually the owner
    in a small deployment — which would leave RLS switched on and doing
    nothing.
    """
    # One statement per execute: asyncpg prepares each statement it is given,
    # and a prepared statement cannot contain several commands.
    _protect("organizations", key="id")
    for table in TENANT_TABLES:
        _protect(table, key="organization_id")


def _protect(table: str, *, key: str) -> None:
    """Enable RLS on one table and install its isolation policy."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    # WITH CHECK as well as USING: without it a tenant could read only its own
    # rows but still write a row stamped with someone else's organization_id.
    op.execute(
        f"CREATE POLICY org_isolation ON {table} FOR ALL "
        f"USING ({key} = {_CURRENT_ORG}) "
        f"WITH CHECK ({key} = {_CURRENT_ORG})"
    )


def downgrade() -> None:
    for table in (*TENANT_TABLES, "organizations"):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("password_resets")
    op.drop_index("audit_log_org_created_idx", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("invitations_org_email_idx", table_name="invitations")
    op.drop_table("invitations")
    op.drop_index("refresh_tokens_user_idx", table_name="refresh_tokens")
    op.drop_index("refresh_tokens_family_idx", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("devices_user_idx", table_name="devices")
    op.drop_table("devices")
    op.drop_index("users_organization_idx", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
