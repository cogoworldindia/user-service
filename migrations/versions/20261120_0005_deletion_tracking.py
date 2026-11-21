"""users.deletion_completed_at: choreographed account deletion (spec §7)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deletion_completed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_users_deletion_pending",
        "users",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL AND deletion_completed_at IS NULL"),
    )
    op.create_index(
        "ix_users_suspension_ends",
        "users",
        ["suspended_until"],
        postgresql_where=sa.text("account_status = 'suspended'"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_suspension_ends", table_name="users")
    op.drop_index("ix_users_deletion_pending", table_name="users")
    op.drop_column("users", "deletion_completed_at")
