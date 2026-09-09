"""Transcripts and their diarised segments (lot L2.5).

Revision ID: 20260909_2000
Revises: 20260909_1700
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_2000"
down_revision: str | None = "20260909_1700"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "current_setting('app.current_org_id', true)::uuid"
_TABLES = ("transcripts", "transcript_segments")


def upgrade() -> None:
    op.create_table(
        "transcripts",
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
        sa.Column("language", sa.String(length=5), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("provider_metadata", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # One per meeting. A retry that wrote a second transcript would leave
        # two versions of the same recording with nothing to choose between.
        sa.UniqueConstraint("meeting_id", name="transcripts_meeting_unique"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transcript_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("speaker_tag", sa.String(length=16), nullable=False),
        sa.Column("speaker_name", sa.String(length=200)),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("channel", sa.String(length=8)),
        sa.CheckConstraint("end_ms >= start_ms", name="transcript_segments_ordered"),
    )
    # Section 19.2: segments are always read in order, for one transcript.
    op.create_index(
        "transcript_segments_transcript_idx", "transcript_segments", ["transcript_id", "start_ms"]
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

    op.drop_index("transcript_segments_transcript_idx", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_table("transcripts")
