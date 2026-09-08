"""Narrow SECURITY DEFINER functions for the two cross-tenant lookups.

Revision ID: 20260908_1000
Revises: 20260908_0900
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260908_1000"
down_revision: str | None = "20260908_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Signing in and refreshing a token are the only two operations that cannot be
# scoped to a tenant: the organization is not known until the user or the token
# has been found. Under RLS those lookups return nothing, so without a way
# through, nobody could ever log in.
#
# The way through is two functions rather than an exemption on `users`. Each
# takes one identifier, returns the few columns authentication needs, and
# nothing else — no name, no email of another account, no way to enumerate. A
# policy exemption on the table would have opened every column to every query.
#
# SECURITY DEFINER runs them with the owner's rights, so `search_path` is
# pinned: without it a caller could put their own `users` table earlier on the
# path and have the function read that instead.


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user(p_email text, p_phone text)
        RETURNS TABLE (
            id uuid,
            organization_id uuid,
            password_hash text,
            revoked_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT u.id, u.organization_id, u.password_hash, u.revoked_at
            FROM users u
            WHERE (p_email IS NOT NULL AND u.email = p_email)
               OR (p_phone IS NOT NULL AND u.phone = p_phone)
            LIMIT 1;
        $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_refresh_token(p_token_hash text)
        RETURNS TABLE (id uuid, organization_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT t.id, t.organization_id
            FROM refresh_tokens t
            WHERE t.token_hash = p_token_hash
            LIMIT 1;
        $$
        """
    )

    # PostgreSQL grants EXECUTE to PUBLIC by default, which on a SECURITY
    # DEFINER function means anyone who can connect. Revoke first, then grant
    # to the one role that needs it.
    for signature in (
        "auth_lookup_user(text, text)",
        "auth_lookup_refresh_token(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO novabrief_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_refresh_token(text)")
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user(text, text)")
