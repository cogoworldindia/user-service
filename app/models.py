from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from cogo_common.db import Base, TimestampMixin
from cogo_common.events.inbox import ProcessedEvent
from cogo_common.events.outbox import OutboxMessage
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class AccountStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    BANNED = "banned"
    DELETED = "deleted"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    NON_BINARY = "non_binary"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class KycLevel(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


class User(TimestampMixin, Base):
    """Spec §4.1. `id` is the user id minted by auth-service. Every change bumps `version`,
    which travels in user.* events so projections can ignore out-of-order updates."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", values_callable=_values),
        server_default=AccountStatus.ACTIVE.value,
    )
    suspended_until: Mapped[datetime | None]
    username: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    name: Mapped[str | None] = mapped_column(String(100))
    dob: Mapped[date | None]
    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="gender", values_callable=_values)
    )
    bio: Mapped[str | None] = mapped_column(String(200))
    profession: Mapped[str | None] = mapped_column(String(60))
    city_name: Mapped[str | None] = mapped_column(String(120))
    city_place_id: Mapped[str | None] = mapped_column(String(255))
    # Plain lat/lng: user-service never runs spatial queries (journey-service projects
    # these into a geography column).
    city_lat: Mapped[float | None] = mapped_column(Float)
    city_lng: Mapped[float | None] = mapped_column(Float)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    personality_tag_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'")
    )
    kyc_level: Mapped[str] = mapped_column(String(10), server_default=KycLevel.NONE.value)
    kyc_prompt_snoozed_until: Mapped[datetime | None]
    guide_seen_at: Mapped[datetime | None]
    followers_count: Mapped[int] = mapped_column(server_default=text("0"))
    following_count: Mapped[int] = mapped_column(server_default=text("0"))
    journeys_count: Mapped[int] = mapped_column(server_default=text("0"))
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    deleted_at: Mapped[datetime | None]

    covers: Mapped[list["UserCoverPhoto"]] = relationship(
        lazy="raise", order_by="UserCoverPhoto.position"
    )

    __table_args__ = (CheckConstraint("kyc_level IN ('none','partial','full')", name="kyc_level"),)


class UserCoverPhoto(Base):
    __tablename__ = "user_cover_photos"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(SmallInteger)
    moderation_hidden: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "position"),
        CheckConstraint("position BETWEEN 0 AND 2", name="position"),
    )


class PersonalityTag(Base):
    __tablename__ = "personality_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(60))
    emoji: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class MediaKind(StrEnum):
    AVATAR = "avatar"
    COVER = "cover"


class MediaStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FLAGGED = "flagged"


class MediaUpload(Base):
    __tablename__ = "media_uploads"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    content_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), server_default=MediaStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('avatar','cover')", name="kind"),
        CheckConstraint("status IN ('pending','ready','flagged')", name="status"),
    )


class BlockProjection(Base):
    """Fed by social.user_blocked/unblocked; unblock keeps a tombstone (active=false)."""

    __tablename__ = "block_projection"

    blocker_id: Mapped[UUID] = mapped_column(primary_key=True)
    blocked_id: Mapped[UUID] = mapped_column(primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    source_version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


__all__ = [
    "AccountStatus",
    "Base",
    "BlockProjection",
    "Gender",
    "KycLevel",
    "MediaUpload",
    "OutboxMessage",
    "PersonalityTag",
    "ProcessedEvent",
    "User",
    "UserCoverPhoto",
]
