"""Cross-tenant lookup for organizations with audio past its retention.

Revision ID: 20260909_2330
Revises: 20260909_2200
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260909_2330"
down_revision: str | None = "20260909_2200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same shape as every other cross-tenant lookup in this system, and the same
# reasoning: the sweep has to know which organizations have work waiting, which
# no scoped session can answer. Read-only, so the destructive part stays inside
# RLS — the job scopes itself to each organization before deleting anything.


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION organizations_with_expired_audio(p_now timestamptz)
        RETURNS TABLE (id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT DISTINCT m.organization_id
            FROM meetings m
            WHERE m.status = 'PUBLISHED'
              AND m.purge_at IS NOT NULL
              AND m.purge_at <= p_now;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION organizations_with_expired_audio(timestamptz) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION organizations_with_expired_audio(timestamptz) TO novabrief_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS organizations_with_expired_audio(timestamptz)")
