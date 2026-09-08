"""Lookup functions for invitation and password-reset links.

Revision ID: 20260908_1700
Revises: 20260908_1000
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260908_1700"
down_revision: str | None = "20260908_1000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Someone following an invitation or a reset link has no session yet, so the
# organization is unknown until the row is found — the same situation as
# signing in, and the same answer: a narrow SECURITY DEFINER function that
# returns an identifier and a tenant, never the token or anything about the
# account behind it.
#
# search_path is pinned on both. Without it a caller could place their own
# table earlier on the path and have the function read that instead.


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_invitation(p_token_hash text)
        RETURNS TABLE (id uuid, organization_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT i.id, i.organization_id
            FROM invitations i
            WHERE i.token_hash = p_token_hash
            LIMIT 1;
        $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_password_reset(p_token_hash text)
        RETURNS TABLE (id uuid, organization_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT r.id, r.organization_id
            FROM password_resets r
            WHERE r.token_hash = p_token_hash
            LIMIT 1;
        $$
        """
    )

    # EXECUTE is granted to PUBLIC by default, which on a SECURITY DEFINER
    # function means anyone able to connect.
    for signature in (
        "auth_lookup_invitation(text)",
        "auth_lookup_password_reset(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO novabrief_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_password_reset(text)")
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_invitation(text)")
