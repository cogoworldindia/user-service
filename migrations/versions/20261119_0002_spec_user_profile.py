"""Phase 1 user model: single users table (spec §4.1), covers, personality tags, media, outbox

Moves user_profiles into users (data preserved), renames status -> account_status (+banned).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TAGS = [
    ("adventure", "Adventure", "⛰️"),
    ("party", "Party", "🎉"),
    ("photography", "Photography", "📷"),
    ("trekking", "Trekking", "🥾"),
    ("foodie", "Foodie", "🍜"),
    ("culture", "Culture", "🏛️"),
    ("spiritual", "Spiritual", "🕉️"),
    ("beach", "Beach", "🏖️"),
    ("roadtrip", "Road trip", "🚗"),
    ("budget", "Budget", "💸"),
    ("luxury", "Luxury", "💎"),
    ("nightlife", "Nightlife", "🌃"),
    ("nature", "Nature", "🌿"),
    ("music", "Music", "🎵"),
    ("reading", "Reading", "📚"),
    ("solo-friendly", "Solo-friendly", "🧭"),
]  # product to finalise (spec §4.1)

GENDER = postgresql.ENUM(name="gender", create_type=False)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.execute("ALTER TYPE user_status RENAME TO account_status")
    op.execute("ALTER TYPE account_status ADD VALUE IF NOT EXISTS 'banned' AFTER 'suspended'")
    op.alter_column("users", "status", new_column_name="account_status")

    columns = [
        sa.Column("suspended_until", TZ),
        sa.Column("username", postgresql.CITEXT()),
        sa.Column("name", sa.String(100)),
        sa.Column("dob", sa.Date()),
        sa.Column("gender", GENDER),
        sa.Column("bio", sa.String(200)),
        sa.Column("profession", sa.String(60)),
        sa.Column("city_name", sa.String(120)),
        sa.Column("city_place_id", sa.String(255)),
        sa.Column("city_lat", sa.Float()),
        sa.Column("city_lng", sa.Float()),
        sa.Column("avatar_url", sa.Text()),
        sa.Column(
            "personality_tag_ids",
            sa.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("kyc_level", sa.String(10), server_default="none", nullable=False),
        sa.Column("kyc_prompt_snoozed_until", TZ),
        sa.Column("guide_seen_at", TZ),
        sa.Column("followers_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("following_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("journeys_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", TZ),
    ]
    for column in columns:
        op.add_column("users", column)
    op.create_unique_constraint(op.f("uq_users_username"), "users", ["username"])
    op.create_check_constraint(
        op.f("ck_users_kyc_level"), "users", "kyc_level IN ('none','partial','full')"
    )

    op.execute(
        """
        UPDATE users u
           SET name = p.full_name, dob = p.date_of_birth, gender = p.gender,
               bio = left(p.bio, 200), avatar_url = p.profile_photo_url
          FROM user_profiles p
         WHERE p.user_id = u.id
        """
    )
    op.drop_table("user_profiles")

    op.create_table(
        "user_cover_photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "moderation_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("position BETWEEN 0 AND 2", name=op.f("ck_user_cover_photos_position")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_cover_photos_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_cover_photos")),
        sa.UniqueConstraint(
            "user_id", "position", name=op.f("uq_user_cover_photos_user_id_position")
        ),
    )
    tags = op.create_table(
        "personality_tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("emoji", sa.String(16)),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personality_tags")),
        sa.UniqueConstraint("slug", name=op.f("uq_personality_tags_slug")),
    )
    op.bulk_insert(
        tags,
        [
            {"slug": s, "label": label, "emoji": e, "sort_order": i}
            for i, (s, label, e) in enumerate(TAGS)
        ],
    )
    op.create_table(
        "media_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(10), server_default="pending", nullable=False),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('avatar','cover')", name=op.f("ck_media_uploads_kind")),
        sa.CheckConstraint(
            "status IN ('pending','ready','flagged')", name=op.f("ck_media_uploads_status")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_media_uploads_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_uploads")),
        sa.UniqueConstraint("object_key", name=op.f("uq_media_uploads_object_key")),
    )
    op.create_index(op.f("ix_media_uploads_user_id"), "media_uploads", ["user_id"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_key", sa.String(200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", TZ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_pending", table_name="outbox", postgresql_where=sa.text("published_at IS NULL")
    )
    op.drop_table("outbox")
    op.drop_index(op.f("ix_media_uploads_user_id"), table_name="media_uploads")
    op.drop_table("media_uploads")
    op.drop_table("personality_tags")
    op.drop_table("user_cover_photos")
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(100)),
        sa.Column("date_of_birth", sa.Date()),
        sa.Column("gender", GENDER),
        sa.Column("bio", sa.String(500)),
        sa.Column("profile_photo_url", sa.String(2048)),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_profiles_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_profiles"),
    )
    op.execute(
        """
        INSERT INTO user_profiles (user_id, full_name, date_of_birth, gender, bio, profile_photo_url)
        SELECT id, name, dob, gender, bio, avatar_url FROM users
         WHERE name IS NOT NULL OR dob IS NOT NULL OR gender IS NOT NULL OR bio IS NOT NULL OR avatar_url IS NOT NULL
        """
    )
    op.drop_constraint(op.f("ck_users_kyc_level"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_username"), "users", type_="unique")
    for name in (
        "deleted_at",
        "version",
        "journeys_count",
        "following_count",
        "followers_count",
        "guide_seen_at",
        "kyc_prompt_snoozed_until",
        "kyc_level",
        "personality_tag_ids",
        "avatar_url",
        "city_lng",
        "city_lat",
        "city_place_id",
        "city_name",
        "profession",
        "bio",
        "gender",
        "dob",
        "name",
        "username",
        "suspended_until",
    ):
        op.drop_column("users", name)
    op.execute("UPDATE users SET account_status = 'suspended' WHERE account_status = 'banned'")
    op.alter_column("users", "account_status", new_column_name="status")
    op.execute(
        "ALTER TYPE account_status RENAME TO user_status"
    )  # 'banned' stays as an unused value
