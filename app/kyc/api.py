"""eKYC endpoints (spec §6.2) and the provider webhook (§9)."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from cogo_common.auth import CurrentUser
from cogo_common.errors import BadRequest, NotFound, Unauthorized
from cogo_common.logging import get_logger
from cogo_common.responses import Envelope, ok
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.kyc.providers import KycProvider
from app.kyc.schemas import (
    EmailStartIn,
    EmailStartOut,
    EmailVerifyIn,
    GovIdStartIn,
    GovIdStartOut,
    KycEmailOut,
    KycGovIdOut,
    KycStatusOut,
    SnoozeOut,
    WebhookAck,
)
from app.kyc.service import KycService, KycState, kyc_prompt
from app.models import KycAttemptStatus, User
from app.schemas import KycPromptOut

log = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["kyc"])


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def kyc_service(
    request: Request, session: Annotated[AsyncSession, Depends(_session)]
) -> KycService:
    state, settings = request.app.state, request.app.state.settings
    return KycService(
        session,
        config=state.config,
        otp=state.kyc_otp,
        provider=state.kyc_provider,
        mailer=state.kyc_mailer,
        return_url=settings.KYC_RETURN_URL,
        gov_id_session_minutes=settings.KYC_GOV_ID_SESSION_MINUTES,
    )


Kyc = Annotated[KycService, Depends(kyc_service)]


def prompt_out(user: User) -> KycPromptOut:
    show, variant = kyc_prompt(user)
    return KycPromptOut(show=show, variant=variant)


def status_out(state: KycState, kyc: KycService) -> KycStatusOut:
    user, now = state.user, datetime.now(UTC)
    email = state.email_attempt
    pending = (
        email
        if email is not None
        and email.status == KycAttemptStatus.OTP_SENT.value
        and email.otp_expires_at is not None
        and email.otp_expires_at > now
        else None
    )
    verified = user.email_verified_at is not None
    return KycStatusOut(
        kyc_level=cast(Literal["none", "partial", "full"], user.kyc_level),
        kyc_prompt=prompt_out(user),
        identity_locked=user.identity_locked,
        email=KycEmailOut(
            status="verified" if verified else ("code_sent" if pending else "not_started"),
            email=user.official_email if verified else None,
            verified_at=user.email_verified_at,
            pending_email=pending.email if pending else None,
            code_expires_at=pending.otp_expires_at if pending else None,
        ),
        gov_id=KycGovIdOut(
            status=kyc.gov_id_status(state, now),
            attempt_id=state.gov_id_attempt.id if state.gov_id_attempt else None,
            id_type=user.gov_id_type,
            last4=user.gov_id_last4,
            verified_at=user.gov_id_verified_at,
        ),
    )


@router.get("/me/kyc", response_model=Envelope[KycStatusOut])
async def get_kyc(principal: CurrentUser, kyc: Kyc) -> Envelope[KycStatusOut]:
    return ok(status_out(await kyc.state(principal.user_id), kyc))


@router.post("/me/kyc/prompt/snooze", response_model=Envelope[SnoozeOut])
async def snooze_prompt(principal: CurrentUser, kyc: Kyc) -> Envelope[SnoozeOut]:
    user = await kyc.snooze(principal.user_id)
    assert user.kyc_prompt_snoozed_until is not None
    return ok(SnoozeOut(kyc_prompt=prompt_out(user), snoozed_until=user.kyc_prompt_snoozed_until))


@router.post("/me/kyc/email/start", status_code=202, response_model=Envelope[EmailStartOut])
async def start_email(
    body: EmailStartIn, principal: CurrentUser, kyc: Kyc
) -> Envelope[EmailStartOut]:
    attempt = await kyc.start_email(principal.user_id, str(body.email))
    assert attempt.email is not None and attempt.otp_expires_at is not None
    return ok(
        EmailStartOut(email=attempt.email, expires_at=attempt.otp_expires_at),
        message="Code sent",
        code=202,
    )


@router.post("/me/kyc/email/verify", response_model=Envelope[KycStatusOut])
async def verify_email(
    body: EmailVerifyIn, principal: CurrentUser, kyc: Kyc
) -> Envelope[KycStatusOut]:
    await kyc.verify_email(principal.user_id, body.otp)
    return ok(status_out(await kyc.state(principal.user_id), kyc), message="Email verified")


@router.post("/me/kyc/gov-id/start", status_code=201, response_model=Envelope[GovIdStartOut])
async def start_gov_id(
    body: GovIdStartIn, principal: CurrentUser, kyc: Kyc
) -> Envelope[GovIdStartOut]:
    attempt, redirect_url = await kyc.start_gov_id(principal.user_id)
    return ok(GovIdStartOut(attempt_id=attempt.id, redirect_url=redirect_url), code=201)


@router.post(
    "/webhooks/kyc/{provider}",
    response_model=Envelope[WebhookAck],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def kyc_webhook(provider: str, request: Request, kyc: Kyc) -> Envelope[WebhookAck]:
    """Provider callback (no user auth): the signature over the raw body is the auth.
    Applied in this request; repeated deliveries are no-ops (spec §9)."""
    active: KycProvider = request.app.state.kyc_provider
    if provider != active.name:
        raise NotFound("Unknown provider")
    body = await request.body()
    if not active.verify_webhook(request.headers, body):
        log.warning("kyc_webhook_bad_signature", provider=provider)
        raise Unauthorized("Invalid signature", error_type="invalid_signature", headers={})
    try:
        payload = json.loads(body)
    except ValueError:
        raise BadRequest("Body must be JSON", error_type="invalid_payload") from None
    if not isinstance(payload, dict):
        raise BadRequest("Body must be a JSON object", error_type="invalid_payload")
    result = await active.parse_result(payload)
    if result is not None:
        await kyc.apply_result(active.name, result)
    return ok(WebhookAck(received=True))
