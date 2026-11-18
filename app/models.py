from datetime import date
from enum import StrEnum
from uuid import UUID

from cogo_common.db import Base, TimestampMixin
from cogo_common.events.inbox import ProcessedEvent
from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    NON_BINARY = "non_binary"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class User(TimestampMixin, Base):
    """`id` is the user id minted by auth-service (same value across services)."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", values_callable=_values),
        server_default=UserStatus.ACTIVE.value,
    )

    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user", lazy="raise", uselist=False
    )


class UserProfile(TimestampMixin, Base):
    """1:1 with users — user_id is the primary key, so a second profile is impossible."""

    __tablename__ = "user_profiles"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str | None] = mapped_column(String(100))
    date_of_birth: Mapped[date | None]
    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="gender", values_callable=_values)
    )
    bio: Mapped[str | None] = mapped_column(String(500))
    profile_photo_url: Mapped[str | None] = mapped_column(String(2048))

    user: Mapped[User] = relationship(back_populates="profile", lazy="raise")


__all__ = ["Base", "Gender", "ProcessedEvent", "User", "UserProfile", "UserStatus"]
