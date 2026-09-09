"""Meetings and their life cycle (lot L2, section 11).

Revision ID: 20260909_1400
Revises: 20260909_1100
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_1400"
down_revision: str | None = "20260909_1100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "current_setting('app.current_org_id', true)::uuid"


def upgrade() -> None:
    op.create_table(
        "meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No cascade: a meeting outlives the account that recorded it. Revoking
        # a member must not erase the organization's minutes.
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200)),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="CREATED"),
        sa.Column("language", sa.String(length=5)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audio_key", sa.String(length=512)),
        sa.Column("audio_sha256", sa.String(length=64)),
        sa.Column("audio_bytes", sa.BigInteger()),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("debug_id", sa.String(length=40), nullable=False),
        sa.Column("provider_stt", sa.String(length=40)),
        sa.Column("provider_llm", sa.String(length=40)),
        sa.Column("purge_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("failed_reason", sa.String(length=64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("duration_seconds >= 0", name="meetings_duration_non_negative"),
        sa.CheckConstraint("paused_seconds >= 0", name="meetings_paused_non_negative"),
        # Per tenant rather than global: the identifier comes from a client we
        # do not control, and a collision between two organizations must not
        # stop one of them from recording.
        sa.UniqueConstraint("organization_id", "debug_id", name="meetings_debug_id_unique"),
    )
    # Descending: every listing is newest first (section 19.2).
    op.execute(
        "CREATE INDEX meetings_org_created_idx ON meetings (organization_id, created_at DESC)"
    )

    op.execute("ALTER TABLE meetings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE meetings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON meetings FOR ALL "
        f"USING (organization_id = {_CURRENT_ORG}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORG})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON meetings")
    op.execute("ALTER TABLE meetings DISABLE ROW LEVEL SECURITY")
    op.drop_index("meetings_org_created_idx", table_name="meetings")
    op.drop_table("meetings")
