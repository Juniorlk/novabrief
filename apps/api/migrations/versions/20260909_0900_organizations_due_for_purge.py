"""Cross-tenant lookup for organizations whose retraction window has expired.

Revision ID: 20260909_0900
Revises: 20260908_1700
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260909_0900"
down_revision: str | None = "20260908_1700"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The purge job has the same problem as signing in: it has to look across every
# tenant, and under RLS a scoped session sees exactly one. The answer is the
# same as `auth_lookup_user` — a narrow SECURITY DEFINER function with a pinned
# search_path.
#
# What is different, and deliberate: this function only *finds*. It returns
# identifiers and deletes nothing. The job then scopes a session to each
# organization in turn and deletes through the ordinary policy, so the one
# irreversible write in the system stays inside RLS instead of being handed a
# way around it.


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION organizations_due_for_purge(p_before timestamptz)
        RETURNS TABLE (id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT o.id
            FROM organizations o
            WHERE o.deletion_requested_at IS NOT NULL
              AND o.deletion_requested_at <= p_before
            ORDER BY o.deletion_requested_at;
        $$
        """
    )

    # EXECUTE is granted to PUBLIC by default, which on a SECURITY DEFINER
    # function means anyone able to connect.
    op.execute("REVOKE ALL ON FUNCTION organizations_due_for_purge(timestamptz) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION organizations_due_for_purge(timestamptz) TO novabrief_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS organizations_due_for_purge(timestamptz)")
