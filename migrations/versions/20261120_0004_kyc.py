"""eKYC: verification columns on users and the kyc_attempts table (spec §4.1, §5.9)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("official_email", postgresql.CITEXT(), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("gov_id_verified_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("gov_id_type", sa.String(length=30)))
    op.add_column("users", sa.Column("gov_id_last4", sa.String(length=4)))
    op.add_column("users", sa.Column("kyc_review_status", sa.String(length=20)))
    op.create_check_constraint(
        op.f("ck_users_kyc_review_status"),
        "users",
        "kyc_review_status IN ('pending_review','rejected')",
    )
    op.create_index(
        "uq_users_verified_official_email",
        "users",
        ["official_email"],
        unique=True,
        postgresql_where=sa.text("email_verified_at IS NOT NULL"),
    )

    op.create_table(
        "kyc_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=True),
        sa.Column("provider_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("otp_hash", sa.String(length=64), nullable=True),
        sa.Column("otp_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("otp_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('email','gov_id')", name=op.f("ck_kyc_attempts_kind")),
        sa.CheckConstraint(
            "status IN ('started','otp_sent','verified','review','failed','expired')",
            name=op.f("ck_kyc_attempts_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_kyc_attempts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kyc_attempts")),
    )
    op.create_index(
        "ix_kyc_attempts_user_kind_created",
        "kyc_attempts",
        ["user_id", "kind", sa.text("created_at DESC")],
    )
    op.create_index(
        "uq_kyc_attempts_provider_ref",
        "kyc_attempts",
        ["provider", "provider_ref"],
        unique=True,
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_kyc_attempts_provider_ref", table_name="kyc_attempts")
    op.drop_index("ix_kyc_attempts_user_kind_created", table_name="kyc_attempts")
    op.drop_table("kyc_attempts")
    op.drop_index("uq_users_verified_official_email", table_name="users")
    op.drop_constraint(op.f("ck_users_kyc_review_status"), "users", type_="check")
    for column in (
        "kyc_review_status",
        "gov_id_last4",
        "gov_id_type",
        "gov_id_verified_at",
        "email_verified_at",
        "official_email",
    ):
        op.drop_column("users", column)
