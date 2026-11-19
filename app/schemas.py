from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

from app.models import AccountStatus, Gender


def _https_only(url: HttpUrl) -> HttpUrl:
    if url.scheme not in ("https", "http") or (
        url.scheme == "http" and url.host not in ("localhost", "127.0.0.1")
    ):
        raise ValueError("must be an https URL")
    return url


def _plausible_birth_date(value: date) -> date:
    if value > date.today() or value.year < 1900:
        raise ValueError("must be a past date after 1900")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Bio = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Profession = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._]{3,30}$"),
    AfterValidator(str.lower),
]
MediaUrl = Annotated[HttpUrl, Field(max_length=2048), AfterValidator(_https_only)]
BirthDate = Annotated[date, AfterValidator(_plausible_birth_date)]


# ---------------------------------------------------------------- spec §6.2: /v1/me
class CityOut(BaseModel):
    name: str
    place_id: str | None = None


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    label: str
    emoji: str | None


class CoverOut(BaseModel):
    position: int
    url: str


class StatsOut(BaseModel):
    journeys: int
    followers: int
    following: int


class KycPromptOut(BaseModel):
    show: bool
    variant: Literal["none", "partial_need_id", "partial_need_email"]


class MeProfileOut(BaseModel):
    """The caller's own profile (GET/PATCH /v1/me)."""

    id: UUID
    username: str | None
    name: str | None
    age: int | None
    dob: date | None
    gender: Gender | None
    bio: str | None
    profession: str | None
    city: CityOut | None
    avatar_url: str | None
    cover_photos: list[CoverOut]
    personality_tags: list[TagOut]
    stats: StatsOut
    kyc_level: str
    kyc_prompt: KycPromptOut
    guide_seen: bool
    account_status: AccountStatus
    created_at: datetime


class MePatch(BaseModel):
    """PATCH /v1/me — omitted fields unchanged, explicit null clears (where allowed)."""

    model_config = ConfigDict(extra="forbid")

    username: Username | None = None
    name: Name | None = None
    dob: BirthDate | None = None
    gender: Gender | None = None
    bio: Bio | None = None
    profession: Profession | None = None
    city_place_id: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    personality_tag_ids: list[int] | None = None
    avatar_url: MediaUrl | None = None

    @field_validator("personality_tag_ids")
    @classmethod
    def _tags(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if len(set(value)) != len(value) or not 3 <= len(value) <= 10:
            raise ValueError("pick 3 to 10 distinct tags")
        return value


class CoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Annotated[int, Field(ge=0, le=2)]
    url: MediaUrl


class PublicProfileOut(BaseModel):
    """Another user's profile (GET /v1/users/{id}). Never DOB (age only), never KYC detail."""

    id: UUID
    username: str | None
    name: str | None
    age: int | None
    avatar_url: str | None
    cover_photos: list[CoverOut]
    bio: str | None
    profession: str | None
    city: CityOut | None
    personality_tags: list[TagOut]
    stats: StatsOut
    kyc_level: str
    is_following: bool | None
    connection_status: Literal["none", "requested", "incoming", "connected", "blocked"] | None


class UploadUrlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["avatar", "cover"]
    content_type: Literal["image/jpeg", "image/png", "image/webp", "image/heic"]


class UploadUrlOut(BaseModel):
    media_id: UUID
    upload_url: str
    file_url: str
    expires_at: datetime
    max_bytes: int


class MediaOut(BaseModel):
    media_id: UUID
    file_url: str
    status: str


# ---------------------------------------------------------------- deprecated (app v0)
class LegacyProfileOut(BaseModel):
    full_name: str | None
    date_of_birth: date | None
    gender: Gender | None
    bio: str | None
    profile_photo_url: str | None
    updated_at: datetime


class LegacyMeOut(BaseModel):
    """Shape of GET /v1/users/me before Phase 1 (kept until the app moves to /v1/me)."""

    id: UUID
    status: AccountStatus
    created_at: datetime
    profile: LegacyProfileOut | None


class LegacyProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Name | None = None
    date_of_birth: BirthDate | None = None
    gender: Gender | None = None
    bio: Bio | None = None
    profile_photo_url: MediaUrl | None = None
