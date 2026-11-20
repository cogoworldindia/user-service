from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.schemas import KycPromptOut


class EmailStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, Field(max_length=254)]


class EmailStartOut(BaseModel):
    email: str
    expires_at: datetime


class EmailVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    otp: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class GovIdStartIn(BaseModel):
    """The consent screen (spec §8.1) must be accepted before a provider session starts."""

    model_config = ConfigDict(extra="forbid")

    consent: Literal[True]


class GovIdStartOut(BaseModel):
    attempt_id: UUID
    redirect_url: str  # open in an in-app browser; it returns to the app's kyc deep link


class SnoozeOut(BaseModel):
    kyc_prompt: KycPromptOut
    snoozed_until: datetime


class KycEmailOut(BaseModel):
    status: Literal["not_started", "code_sent", "verified"]
    email: str | None  # the verified address
    verified_at: datetime | None
    pending_email: str | None  # a code was sent here and is still valid
    code_expires_at: datetime | None


class KycGovIdOut(BaseModel):
    status: Literal["not_started", "pending", "verified", "review", "rejected", "failed", "expired"]
    attempt_id: UUID | None  # latest session
    id_type: str | None
    last4: str | None
    verified_at: datetime | None


class KycStatusOut(BaseModel):
    """GET /v1/me/kyc: each check's state, for the eKYC flow (spec §8.1) and its polling."""

    kyc_level: Literal["none", "partial", "full"]
    kyc_prompt: KycPromptOut
    identity_locked: bool
    email: KycEmailOut
    gov_id: KycGovIdOut


class WebhookAck(BaseModel):
    received: bool
