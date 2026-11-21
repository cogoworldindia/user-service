"""eKYC (spec §5.9): official-email OTP, government-ID sessions and their results, the
derived `kyc_level`, and the post-login prompt. Everything KYC lives here; only the level
leaves user-service (`user.kyc_level_changed`)."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from cogo_common.appconfig import AppConfig
from cogo_common.errors import (
    AppError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    ServiceUnavailable,
)
from cogo_common.events.contracts import UserKycLevelChanged
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.outbox import enqueue
from cogo_common.events.topology import RoutingKeys
from cogo_common.ids import new_id
from cogo_common.kyc import derive_kyc_level
from cogo_common.logging import get_logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.kyc.mailer import KycMailer
from app.kyc.names import name_similarity
from app.kyc.providers import KycProvider, KycResult
from app.models import AccountStatus, KycAttempt, KycAttemptKind, KycAttemptStatus, KycLevel, User
from app.service import PRODUCER, UserService, snapshot

log = get_logger(__name__)

_TERMINAL = (KycAttemptStatus.VERIFIED, KycAttemptStatus.REVIEW, KycAttemptStatus.FAILED)

PromptVariant = Literal["none", "partial_need_id", "partial_need_email"]
GovIdStatus = Literal[
    "not_started", "pending", "verified", "review", "rejected", "failed", "expired"
]


@dataclass(frozen=True, slots=True)
class OtpPolicy:
    hmac_key: bytes
    ttl_seconds: int = 600
    max_attempts: int = 5
    sends_per_hour: int = 3


@dataclass(frozen=True, slots=True)
class KycState:
    user: User
    email_attempt: KycAttempt | None  # latest
    gov_id_attempt: KycAttempt | None  # latest


def kyc_prompt(user: User, now: datetime | None = None) -> tuple[bool, PromptVariant]:
    """(show, variant) for `GET /me` (spec §5.9): show unless full, snoozed, or an ID is
    already waiting for review."""
    now = now or datetime.now(UTC)
    snoozed = user.kyc_prompt_snoozed_until is not None and user.kyc_prompt_snoozed_until > now
    show = (
        user.kyc_level != KycLevel.FULL.value
        and not snoozed
        and user.kyc_review_status != "pending_review"
    )
    variant: PromptVariant
    if user.kyc_level == KycLevel.NONE.value:
        variant = "none"
    else:  # partial: whichever check is still missing
        variant = "partial_need_id" if user.gov_id_verified_at is None else "partial_need_email"
    return show, variant


def email_domain_blocked(email: str, blocked: list[str]) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith(f".{d}") for d in (b.lower() for b in blocked))


async def relevel(session: AsyncSession, user: User) -> None:
    """Recompute the level (spec §5.9); on a change bump the version and emit (inside the
    caller's transaction, user row locked)."""
    level = derive_kyc_level(
        email_verified=user.email_verified_at is not None,
        gov_id_verified=user.gov_id_verified_at is not None,
    )
    if level == user.kyc_level:
        return
    previous, user.kyc_level = user.kyc_level, level
    user.version += 1
    user.updated_at = datetime.now(UTC)
    await session.flush()
    payload = UserKycLevelChanged(
        **snapshot(user).model_dump(), previous_kyc_level=previous
    ).model_dump(mode="json")
    enqueue(
        session,
        EventEnvelope(type=RoutingKeys.USER_KYC_LEVEL_CHANGED, producer=PRODUCER, data=payload),
    )
    log.info("kyc_level_changed", user_id=str(user.id), kyc_level=level, previous=previous)


class KycService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        config: AppConfig,
        otp: OtpPolicy,
        provider: KycProvider,
        mailer: KycMailer,
        return_url: str,
        gov_id_session_minutes: int = 30,
    ) -> None:
        self._session = session
        self._config = config
        self._otp = otp
        self._provider = provider
        self._mailer = mailer
        self._return_url = return_url
        self._session_minutes = gov_id_session_minutes

    # ------------------------------------------------------------ helpers (inside a tx)
    async def _user(self, user_id: UUID) -> User:
        await UserService(self._session).ensure_user(user_id)
        user = await self._session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert user is not None
        if user.account_status != AccountStatus.ACTIVE:
            raise Forbidden("Account is not active", error_type="account_inactive")
        return user

    async def _latest(self, user_id: UUID, kind: KycAttemptKind) -> KycAttempt | None:
        return await self._session.scalar(
            select(KycAttempt)
            .where(KycAttempt.user_id == user_id, KycAttempt.kind == kind.value)
            .order_by(KycAttempt.created_at.desc(), KycAttempt.id.desc())
            .limit(1)
        )

    async def _count_since(self, user_id: UUID, kind: KycAttemptKind, since: datetime) -> int:
        return int(
            await self._session.scalar(
                select(func.count()).where(
                    KycAttempt.user_id == user_id,
                    KycAttempt.kind == kind.value,
                    KycAttempt.created_at > since,
                )
            )
            or 0
        )

    async def _supersede(self, user_id: UUID, kind: KycAttemptKind, status: str) -> None:
        await self._session.execute(
            update(KycAttempt)
            .where(
                KycAttempt.user_id == user_id,
                KycAttempt.kind == kind.value,
                KycAttempt.status == status,
            )
            .values(status=KycAttemptStatus.EXPIRED.value)
        )

    async def _relevel(self, user: User) -> None:
        await relevel(self._session, user)

    def _code_hash(self, attempt_id: UUID, code: str) -> str:
        return hmac.new(
            self._otp.hmac_key, f"{attempt_id}:{code}".encode(), hashlib.sha256
        ).hexdigest()

    # ------------------------------------------------------------ status + prompt
    async def state(self, user_id: UUID) -> KycState:
        async with self._session.begin():
            await UserService(self._session).ensure_user(user_id)
            user = await self._session.get(User, user_id, populate_existing=True)
            assert user is not None
            return KycState(
                user,
                await self._latest(user_id, KycAttemptKind.EMAIL),
                await self._latest(user_id, KycAttemptKind.GOV_ID),
            )

    def gov_id_status(self, state: KycState, now: datetime | None = None) -> GovIdStatus:
        now = now or datetime.now(UTC)
        user, attempt = state.user, state.gov_id_attempt
        if user.gov_id_verified_at is not None:
            return "verified"
        if user.kyc_review_status == "pending_review":
            return "review"
        if user.kyc_review_status == "rejected":
            return "rejected"
        if attempt is None:
            return "not_started"
        if attempt.status == KycAttemptStatus.STARTED.value:
            fresh = attempt.created_at > now - timedelta(minutes=self._session_minutes)
            return "pending" if fresh else "expired"
        return cast(GovIdStatus, attempt.status)

    async def snooze(self, user_id: UUID) -> User:
        days = int((await self._config.get("kyc"))["prompt_cooldown_days"])
        async with self._session.begin():
            user = await self._user(user_id)
            user.kyc_prompt_snoozed_until = datetime.now(UTC) + timedelta(days=days)
        return user

    # ------------------------------------------------------------ official email
    async def start_email(self, user_id: UUID, email: str) -> KycAttempt:
        email = email.strip().lower()
        cfg = await self._config.get("kyc")
        if cfg["require_official_domain"] and email_domain_blocked(
            email, list(cfg["blocked_email_domains"])
        ):
            raise BadRequest(
                "Use your work or college email address", error_type="email_domain_not_allowed"
            )
        now = datetime.now(UTC)
        code = f"{secrets.randbelow(1_000_000):06d}"
        async with self._session.begin():
            user = await self._user(user_id)  # row lock: concurrent sends count correctly
            if user.email_verified_at is not None and user.official_email == email:
                raise Conflict("This email is already verified", error_type="already_verified")
            oldest = await self._session.scalar(
                select(func.min(KycAttempt.created_at)).where(
                    KycAttempt.user_id == user_id,
                    KycAttempt.kind == KycAttemptKind.EMAIL.value,
                    KycAttempt.created_at > now - timedelta(hours=1),
                )
            )
            sent = await self._count_since(user_id, KycAttemptKind.EMAIL, now - timedelta(hours=1))
            if sent >= self._otp.sends_per_hour and oldest is not None:
                raise RateLimited(
                    "Too many codes requested; try again later",
                    error_type="otp_send_limit",
                    retry_after=int((oldest + timedelta(hours=1) - now).total_seconds()) + 1,
                )
            await self._supersede(user_id, KycAttemptKind.EMAIL, KycAttemptStatus.OTP_SENT.value)
            attempt = KycAttempt(
                id=new_id(),
                user_id=user_id,
                kind=KycAttemptKind.EMAIL.value,
                status=KycAttemptStatus.OTP_SENT.value,
                email=email,
                otp_expires_at=now + timedelta(seconds=self._otp.ttl_seconds),
            )
            attempt.otp_hash = self._code_hash(attempt.id, code)
            self._session.add(attempt)
        try:
            await self._mailer.send_code(email, code, ttl_seconds=self._otp.ttl_seconds)
        except Exception:
            async with self._session.begin():  # not sent: don't count it against the limit
                await self._session.execute(delete(KycAttempt).where(KycAttempt.id == attempt.id))
            raise
        log.info("kyc_email_code_sent", user_id=str(user_id), attempt_id=str(attempt.id))
        return attempt

    async def verify_email(self, user_id: UUID, code: str) -> User:
        now = datetime.now(UTC)
        failure: AppError | None = None  # raised after commit: a wrong code must still count
        async with self._session.begin():
            user = await self._user(user_id)
            attempt = await self._session.scalar(
                select(KycAttempt)
                .where(
                    KycAttempt.user_id == user_id,
                    KycAttempt.kind == KycAttemptKind.EMAIL.value,
                    KycAttempt.status == KycAttemptStatus.OTP_SENT.value,
                )
                .order_by(KycAttempt.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if attempt is None:
                raise BadRequest("Request a new code", error_type="otp_invalid")
            if attempt.otp_expires_at is None or attempt.otp_expires_at <= now:
                attempt.status = KycAttemptStatus.EXPIRED.value
                failure = BadRequest(
                    "This code has expired; request a new one", error_type="otp_expired"
                )
            elif not hmac.compare_digest(attempt.otp_hash or "", self._code_hash(attempt.id, code)):
                attempt.otp_attempts += 1
                left = self._otp.max_attempts - attempt.otp_attempts
                if left <= 0:
                    attempt.status = KycAttemptStatus.FAILED.value
                    failure = RateLimited(  # same status as sign-in's otp_locked
                        "Too many incorrect attempts; request a new code", error_type="otp_locked"
                    )
                else:
                    failure = BadRequest(
                        "Incorrect code", error_type="otp_invalid", details={"attempts_left": left}
                    )
            else:
                attempt.status = KycAttemptStatus.VERIFIED.value
                user.official_email = attempt.email
                user.email_verified_at = now
                try:
                    await self._session.flush()
                except IntegrityError:
                    raise Conflict(
                        "This email is verified on another account", error_type="email_in_use"
                    ) from None
                await self._relevel(user)
        if failure is not None:
            raise failure
        log.info("kyc_email_verified", user_id=str(user_id))
        return user

    # ------------------------------------------------------------ government ID
    async def start_gov_id(self, user_id: UUID) -> tuple[KycAttempt, str]:
        limit = int((await self._config.get("kyc"))["gov_id_attempts_per_day"])
        now = datetime.now(UTC)
        async with self._session.begin():
            user = await self._user(user_id)
            if user.gov_id_verified_at is not None:
                raise Conflict("Your ID is already verified", error_type="already_verified")
            if user.kyc_review_status == "pending_review":
                raise Conflict("Your ID is being reviewed", error_type="kyc_review_pending")
            if not user.name or user.dob is None:
                raise BadRequest(
                    "Add your name and date of birth first", error_type="profile_incomplete"
                )
            day = await self._count_since(user_id, KycAttemptKind.GOV_ID, now - timedelta(days=1))
            if day >= limit:
                raise RateLimited(
                    "Too many verification attempts today",
                    error_type="kyc_attempt_limit",
                    retry_after=3600,
                )
            await self._supersede(user_id, KycAttemptKind.GOV_ID, KycAttemptStatus.STARTED.value)
            user.kyc_review_status = None  # a fresh check after a rejected review
            attempt = KycAttempt(
                id=new_id(),
                user_id=user_id,
                kind=KycAttemptKind.GOV_ID.value,
                provider=self._provider.name,
                status=KycAttemptStatus.STARTED.value,
                consent_at=now,
            )
            self._session.add(attempt)
        # The provider call runs outside any transaction (no row lock held over the network).
        try:
            session = await self._provider.start_session(
                attempt_id=attempt.id, return_url=f"{self._return_url}?attempt_id={attempt.id}"
            )
        except Exception as exc:
            async with self._session.begin():
                await self._session.execute(
                    update(KycAttempt)
                    .where(KycAttempt.id == attempt.id)
                    .values(status=KycAttemptStatus.FAILED.value)
                )
            log.error("kyc_session_failed", provider=self._provider.name, error=type(exc).__name__)
            if isinstance(exc, AppError):
                raise
            raise ServiceUnavailable(
                "ID verification is unavailable right now, please retry",
                error_type="kyc_provider_unavailable",
            ) from exc
        async with self._session.begin():
            await self._session.execute(
                update(KycAttempt)
                .where(KycAttempt.id == attempt.id)
                .values(provider_ref=session.provider_ref)
            )
        log.info("kyc_session_started", user_id=str(user_id), attempt_id=str(attempt.id))
        return attempt, session.redirect_url

    async def apply_result(self, provider: str, result: KycResult) -> str | None:
        """Apply a provider result (webhook). Idempotent: a repeated delivery for a finished
        attempt changes nothing. Returns the attempt's new status (None if ignored)."""
        threshold = float((await self._config.get("kyc"))["name_match_threshold"])
        async with self._session.begin():
            attempt = await self._session.scalar(
                select(KycAttempt)
                .where(
                    KycAttempt.provider == provider, KycAttempt.provider_ref == result.provider_ref
                )
                .with_for_update()
            )
            if attempt is None:
                log.warning("kyc_result_unknown_ref", provider=provider)
                return None
            if attempt.status in _TERMINAL:
                return None
            # A result for a superseded ('expired') session is still a genuine check of this
            # user, so it's applied like a current one.
            user = await self._session.scalar(
                select(User).where(User.id == attempt.user_id).with_for_update()
            )
            if user is None:
                return None
            if not result.success:
                attempt.status = KycAttemptStatus.FAILED.value
                log.info("kyc_result_failed", attempt_id=str(attempt.id))
                return attempt.status
            name_match = name_similarity(user.name, result.name) >= threshold
            dob_match = user.dob is not None and user.dob == result.dob
            last4 = (result.last4 or "")[-4:] or None
            attempt.result = {
                "name_match": name_match,
                "dob_match": dob_match,
                "id_type": result.id_type,
                "last4": last4,
            }
            if user.gov_id_verified_at is not None:  # already verified by another session
                attempt.status = KycAttemptStatus.VERIFIED.value
            elif name_match and dob_match:
                attempt.status = KycAttemptStatus.VERIFIED.value
                user.gov_id_verified_at = datetime.now(UTC)
                user.gov_id_type, user.gov_id_last4 = result.id_type, last4
                user.kyc_review_status = None
                await self._relevel(user)
            else:  # a genuine ID that doesn't match the profile: an admin decides (M7)
                attempt.status = KycAttemptStatus.REVIEW.value
                user.kyc_review_status = "pending_review"
            log.info(
                "kyc_result_applied",
                attempt_id=str(attempt.id),
                status=attempt.status,
                name_match=name_match,
                dob_match=dob_match,
            )
            return attempt.status

    async def attempt_for_page(self, provider: str, provider_ref: str) -> tuple[KycAttempt, User]:
        """The mock provider's hosted page looks up its session (development only)."""
        async with self._session.begin():
            attempt = await self._session.scalar(
                select(KycAttempt).where(
                    KycAttempt.provider == provider, KycAttempt.provider_ref == provider_ref
                )
            )
            if attempt is None:
                raise NotFound("Session not found")
            user = await self._session.get(User, attempt.user_id)
            assert user is not None
            return attempt, user


def result_summary(attempt: KycAttempt | None) -> dict[str, Any] | None:
    return dict(attempt.result) if attempt is not None and attempt.result else None
