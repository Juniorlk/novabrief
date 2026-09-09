"""Email verification at signup (EF-02).

Revision ID: 20260909_1100
Revises: 20260909_0900
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_1100"
down_revision: str | None = "20260909_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "current_setting('app.current_org_id', true)::uuid"


def upgrade() -> None:
    op.create_table(
        "email_verifications",
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
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token_hash", name="email_verifications_token_unique"),
    )
    op.create_index(
        "email_verifications_user_idx", "email_verifications", ["user_id", "created_at"]
    )

    # Same treatment as every other tenant table (ADR-04). FORCE as well as
    # ENABLE: without it the table owner is exempt from its own policy.
    op.execute("ALTER TABLE email_verifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_verifications FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON email_verifications FOR ALL "
        f"USING (organization_id = {_CURRENT_ORG}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORG})"
    )

    # Someone following a verification link has no session: the organization is
    # unknown until the row is found. The same situation as signing in, and the
    # same answer — a narrow SECURITY DEFINER function with a pinned
    # search_path, returning an identifier and a tenant and nothing else.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_email_verification(p_token_hash text)
        RETURNS TABLE (id uuid, organization_id uuid)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT v.id, v.organization_id
            FROM email_verifications v
            WHERE v.token_hash = p_token_hash
            LIMIT 1;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_lookup_email_verification(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION auth_lookup_email_verification(text) TO novabrief_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_email_verification(text)")
    op.execute("DROP POLICY IF EXISTS org_isolation ON email_verifications")
    op.execute("ALTER TABLE email_verifications DISABLE ROW LEVEL SECURITY")
    op.drop_index("email_verifications_user_idx", table_name="email_verifications")
    op.drop_table("email_verifications")
