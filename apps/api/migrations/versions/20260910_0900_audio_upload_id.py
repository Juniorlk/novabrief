"""Remember the open multipart upload so it can be aborted.

An upload that is neither completed nor aborted leaves its parts in the bucket
and is billed for them indefinitely. Aborting one needs the identifier the
store issued when it opened, and nothing recorded it — `cancel` took a storage
provider and could not use it.

Nullable, and null is the normal state: the column holds a value only between
`finalize-local` and `finalize`.

Revision ID: 20260910_0900
Revises: 20260909_2330
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_0900"
down_revision: str | None = "20260909_2330"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("audio_upload_id", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "audio_upload_id")
