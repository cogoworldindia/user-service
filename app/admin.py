"""What admin-service does to users through `/internal/admin/*` (spec §5.10, §6.4): search
and inspect, suspend / ban / reinstate, KYC reviews, and taking down a photo."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from cogo_common.errors import BadRequest, Conflict, NotFound
from cogo_common.events.contracts import MediaFlagged, UserStatusChanged
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.outbox import enqueue
from cogo_common.events.topology import RoutingKeys
from cogo_common.logging import get_logger
from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.kyc.service import relevel
from app.media import MediaStore
from app.models import (
    AccountStatus,
    KycAttempt,
    KycAttemptKind,
    KycAttemptStatus,
    MediaStatus,
    MediaUpload,
    User,
    UserCoverPhoto,
)
from app.service import PRODUCER, snapshot

log = get_logger(__name__)
StatusAction = Literal["suspend", "ban", "reinstate"]


def emit_status_changed(session: AsyncSession, user: User, previous: str) -> None:
    payload = UserStatusChanged(
        **snapshot(user).model_dump(),
        previous_status=previous,
        suspended_until=user.suspended_until,
    ).model_dump(mode="json")
    enqueue(
        session,
        EventEnvelope(type=RoutingKeys.USER_STATUS_CHANGED, producer=PRODUCER, data=payload),
    )


async def take_down_media(
    session: AsyncSession,
    upload: MediaUpload,
    *,
    media: MediaStore,
    labels: dict[str, str] | None = None,
) -> None:
    """Flag a photo and stop showing it (spec §5.10: "replace with a placeholder"; the app
    renders its default avatar for a null avatar_url). With `labels` (image moderation),
    also emit media.flagged so social-service opens a system report."""
    upload.status = MediaStatus.FLAGGED.value
    url = media.public_url(upload.object_key)
    user = await session.scalar(
        select(User)
        .options(selectinload(User.covers))
        .where(User.id == upload.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        return
    await session.execute(
        delete(UserCoverPhoto).where(UserCoverPhoto.user_id == user.id, UserCoverPhoto.url == url)
    )
    if user.avatar_url == url:
        user.avatar_url = None
        user.version += 1
        user.updated_at = datetime.now(UTC)
        await session.flush()
        enqueue(
            session,
            EventEnvelope(
                type=RoutingKeys.USER_UPDATED,
                producer=PRODUCER,
                data=snapshot(user).model_dump(mode="json"),
            ),
        )
    if labels is not None:
        enqueue(
            session,
            EventEnvelope(
                type=RoutingKeys.MEDIA_FLAGGED,
                producer=PRODUCER,
                data=MediaFlagged(
                    media_id=upload.id,
                    owner_id=upload.user_id,
                    kind=cast(Literal["avatar", "cover"], upload.kind),
                    media_url=url,
                    labels=labels,
                ).model_dump(mode="json"),
            ),
        )
    log.info(
        "media_taken_down", media_id=str(upload.id), kind=upload.kind, flagged=labels is not None
    )


class AdminUsers:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(self, q: str) -> list[User]:
        q = q.strip()
        try:
            stmt = select(User).where(User.id == UUID(q))
        except ValueError:
            if len(q) < 2:
                raise BadRequest(
                    "Type at least 2 characters", error_type="query_too_short"
                ) from None
            prefix = q.replace("\\", "").replace("%", "").replace("_", r"\_") + "%"
            stmt = (
                select(User)
                .where(or_(User.username.ilike(prefix), User.name.ilike(prefix)))
                .order_by(User.username)
                .limit(20)
            )
        return list((await self._session.scalars(stmt)).all())

    async def get(self, user_id: UUID) -> User:
        user = await self._session.scalar(
            select(User)
            .options(selectinload(User.covers))
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
        )
        if user is None:
            raise NotFound("User not found")
        return user

    async def set_status(
        self, user_id: UUID, action: StatusAction, *, until: datetime | None, admin_id: UUID
    ) -> User:
        now = datetime.now(UTC)
        async with self._session.begin():
            user = await self._session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise NotFound("User not found")
            previous = user.account_status.value
            if user.account_status == AccountStatus.DELETED:
                raise Conflict("The account is deleted", error_type="invalid_status")
            if action == "suspend":
                if until is None or until <= now:
                    raise BadRequest("Pick a time in the future", error_type="invalid_until")
                user.account_status, user.suspended_until = AccountStatus.SUSPENDED, until
            elif action == "ban":
                user.account_status, user.suspended_until = AccountStatus.BANNED, None
            else:
                if user.account_status == AccountStatus.ACTIVE:
                    raise Conflict("The account is already active", error_type="invalid_status")
                user.account_status, user.suspended_until = AccountStatus.ACTIVE, None
            user.version += 1
            user.updated_at = now
            await self._session.flush()
            emit_status_changed(self._session, user, previous)
        log.info("user_status_set", user_id=str(user_id), action=action, admin_id=str(admin_id))
        return await self.get(user_id)

    async def expire_suspensions(self, limit: int = 100) -> int:
        """Reinstate suspensions that ran out (a scheduled job; replicas can run it at the
        same time: SKIP LOCKED gives each row to one of them)."""
        async with self._session.begin():
            users = (
                await self._session.scalars(
                    select(User)
                    .where(
                        User.account_status == AccountStatus.SUSPENDED,
                        User.suspended_until <= datetime.now(UTC),
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for user in users:
                user.account_status, user.suspended_until = AccountStatus.ACTIVE, None
                user.version += 1
                user.updated_at = datetime.now(UTC)
                await self._session.flush()
                emit_status_changed(self._session, user, AccountStatus.SUSPENDED.value)
        if users:
            log.info("suspensions_expired", count=len(users))
        return len(users)

    # ------------------------------------------------------------ KYC reviews (spec §5.9)
    async def kyc_reviews(
        self, *, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[tuple[User, KycAttempt]]:
        stmt = (
            select(User, KycAttempt)
            .join(KycAttempt, KycAttempt.user_id == User.id)
            .where(
                User.kyc_review_status == "pending_review",
                KycAttempt.kind == KycAttemptKind.GOV_ID.value,
                KycAttempt.status == KycAttemptStatus.REVIEW.value,
            )
            .order_by(KycAttempt.updated_at, KycAttempt.id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(tuple_(KycAttempt.updated_at, KycAttempt.id) > tuple_(*after))
        return [(u, a) for u, a in (await self._session.execute(stmt)).all()]

    async def review(self, user_id: UUID, *, approve: bool, admin_id: UUID) -> User:
        async with self._session.begin():
            user = await self._session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise NotFound("User not found")
            if user.kyc_review_status != "pending_review":
                raise Conflict("No ID is waiting for review", error_type="not_in_review")
            attempt = await self._session.scalar(
                select(KycAttempt)
                .where(
                    KycAttempt.user_id == user_id,
                    KycAttempt.kind == KycAttemptKind.GOV_ID.value,
                    KycAttempt.status == KycAttemptStatus.REVIEW.value,
                )
                .order_by(KycAttempt.updated_at.desc())
                .limit(1)
                .with_for_update()
            )
            result = (attempt.result if attempt else None) or {}
            if approve:
                user.gov_id_verified_at = datetime.now(UTC)
                user.gov_id_type = result.get("id_type")  # type: ignore[assignment]
                user.gov_id_last4 = result.get("last4")  # type: ignore[assignment]
                user.kyc_review_status = None
                if attempt is not None:
                    attempt.status = KycAttemptStatus.VERIFIED.value
                await relevel(self._session, user)
            else:  # the user can fix their profile and try again
                user.kyc_review_status = "rejected"
                if attempt is not None:
                    attempt.status = KycAttemptStatus.FAILED.value
        log.info("kyc_reviewed", user_id=str(user_id), approved=approve, admin_id=str(admin_id))
        return await self.get(user_id)

    # ------------------------------------------------------------ photos
    async def media(self, media_id: UUID) -> MediaUpload:
        upload = await self._session.get(MediaUpload, media_id)
        if upload is None:
            raise NotFound("Photo not found")
        return upload

    async def remove_media(self, media_id: UUID, *, media: MediaStore, admin_id: UUID) -> None:
        async with self._session.begin():
            upload = await self._session.get(MediaUpload, media_id, with_for_update=True)
            if upload is None:
                raise NotFound("Photo not found")
            if upload.status != MediaStatus.FLAGGED.value:
                await take_down_media(self._session, upload, media=media)
        log.info("media_removed", media_id=str(media_id), admin_id=str(admin_id))
