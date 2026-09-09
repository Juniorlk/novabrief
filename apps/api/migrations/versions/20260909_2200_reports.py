"""Reports, decisions and tasks (lot L2.6).

Revision ID: 20260909_2200
Revises: 20260909_2000
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_2200"
down_revision: str | None = "20260909_2000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "current_setting('app.current_org_id', true)::uuid"
_TABLES = ("reports", "decisions", "tasks")


def _tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def _review_columns() -> list[sa.Column]:
    """Section 19.1: what a person did with an extracted item."""
    return [
        sa.Column(
            "human_status", sa.String(length=16), nullable=False, server_default="UNREVIEWED"
        ),
        sa.Column("edited_content", sa.Text()),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    ]


def upgrade() -> None:
    op.create_table(
        "reports",
        *_tenant_columns(),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("participants", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("summary", postgresql.JSONB(), nullable=False, server_default="[]"),
        # Section 18.6 evaluates every change of model or prompt; a regression
        # cannot be traced back without both recorded next to the output.
        sa.Column("model_version", sa.String(length=64)),
        sa.Column("prompt_version", sa.String(length=32)),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("meeting_id", name="reports_meeting_unique"),
    )

    op.create_table(
        "decisions",
        *_tenant_columns(),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_start_ms", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        *_review_columns(),
    )
    op.create_index("decisions_meeting_idx", "decisions", ["meeting_id", "source_start_ms"])

    op.create_table(
        "tasks",
        *_tenant_columns(),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("assignee_name", sa.String(length=200)),
        # SET NULL, not CASCADE: a task outlives the person who left, and the
        # organization still needs to know it exists.
        sa.Column(
            "assignee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("deadline_text", sa.String(length=120)),
        sa.Column("deadline_date", sa.Date()),
        sa.Column("source_start_ms", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        *_review_columns(),
    )
    op.create_index(
        "tasks_assignee_idx", "tasks", ["organization_id", "assignee_user_id", "human_status"]
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY org_isolation ON {table} FOR ALL "
            f"USING (organization_id = {_CURRENT_ORG}) "
            f"WITH CHECK (organization_id = {_CURRENT_ORG})"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("tasks_assignee_idx", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("decisions_meeting_idx", table_name="decisions")
    op.drop_table("decisions")
    op.drop_table("reports")
