"""Consumption ledger and a quota that can be absent (lot L2.3).

Revision ID: 20260909_1700
Revises: 20260909_1400
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_1700"
down_revision: str | None = "20260909_1400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "current_setting('app.current_org_id', true)::uuid"


def upgrade() -> None:
    # "No plan assigned yet" and "a quota of zero seconds" are different facts.
    # Stored as the same 0, every meeting would sit in QUOTA_HOLD from the day
    # quota enforcement lands until billing exists to lift it.
    op.alter_column("organizations", "quota_seconds", nullable=True)
    op.execute("UPDATE organizations SET quota_seconds = NULL WHERE quota_seconds = 0")

    op.create_table(
        "usage_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # SET NULL rather than the reference DDL's RESTRICT. RESTRICT would make
        # an organization undeletable and contradict EF-06, whose acceptance
        # criterion is that nothing survives a deletion. Releasing the reference
        # keeps the aggregate cost history Novafrik needs while the row stops
        # naming the customer who asked to be forgotten.
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
        ),
        # Section 19.2 asks for exactly this: the meeting goes, the accounting
        # record stays.
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="SET NULL"),
        ),
        sa.Column("seconds_billed", sa.Integer(), nullable=False),
        sa.Column("stt_provider", sa.String(length=40)),
        sa.Column("stt_cost_usd", sa.Numeric(10, 5), nullable=False, server_default="0"),
        sa.Column("llm_provider", sa.String(length=40)),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_cost_usd", sa.Numeric(10, 5), nullable=False, server_default="0"),
        sa.Column("storage_cost_usd", sa.Numeric(10, 5), nullable=False, server_default="0"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("seconds_billed >= 0", name="usage_ledger_seconds_non_negative"),
    )
    op.create_index(
        "usage_ledger_org_recorded_idx", "usage_ledger", ["organization_id", "recorded_at"]
    )

    op.execute("ALTER TABLE usage_ledger ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_ledger FORCE ROW LEVEL SECURITY")
    # A released row (organization_id NULL) matches no tenant and is therefore
    # invisible to every customer, which is the intent: once detached it is
    # Novafrik's accounting, reachable only through the back-office.
    op.execute(
        "CREATE POLICY org_isolation ON usage_ledger FOR ALL "
        f"USING (organization_id = {_CURRENT_ORG}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORG})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON usage_ledger")
    op.execute("ALTER TABLE usage_ledger DISABLE ROW LEVEL SECURITY")
    op.drop_index("usage_ledger_org_recorded_idx", table_name="usage_ledger")
    op.drop_table("usage_ledger")

    op.execute("UPDATE organizations SET quota_seconds = 0 WHERE quota_seconds IS NULL")
    op.alter_column("organizations", "quota_seconds", nullable=False)
