from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.models import Gender, UserStatus


def _https_only(url: HttpUrl) -> HttpUrl:
    if url.scheme != "https":
        raise ValueError("must be an https URL")
    return url


def _plausible_birth_date(value: date) -> date:
    if value > date.today() or value.year < 1900:
        raise ValueError("must be a past date after 1900")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Bio = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
PhotoUrl = Annotated[HttpUrl, Field(max_length=2048), AfterValidator(_https_only)]
BirthDate = Annotated[date, AfterValidator(_plausible_birth_date)]


class ProfileUpdate(BaseModel):
    """PATCH semantics: omitted fields are untouched, explicit `null` clears a field.
    `status` and ids are not accepted — users cannot change them."""

    model_config = ConfigDict(extra="forbid")

    full_name: Name | None = None
    date_of_birth: BirthDate | None = None
    gender: Gender | None = None
    bio: Bio | None = None
    profile_photo_url: PhotoUrl | None = None


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str | None
    date_of_birth: date | None
    gender: Gender | None
    bio: str | None
    profile_photo_url: str | None
    updated_at: datetime


class MeOut(BaseModel):
    """Everything about the caller — only ever returned to the owner."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: UserStatus
    created_at: datetime
    profile: ProfileOut | None


class PublicUserOut(BaseModel):
    """What other users may see. No date of birth, gender or status."""

    id: UUID
    full_name: str | None
    bio: str | None
    profile_photo_url: str | None
