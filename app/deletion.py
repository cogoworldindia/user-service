"""Account deletion (spec §7, choreographed): user-service anonymises its own row and emits
`user.deleted`; every other service cleans up its own data when it consumes it. A tracker
asks each one (`GET /internal/admin/deletion-status/{id}`) until all report done, and
alerts if that takes longer than a day."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cogo_common.errors import NotFound
from cogo_common.events.contracts import UserDeleted
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.outbox import enqueue
from cogo_common.events.topology import RoutingKeys
from cogo_common.internal_http import InternalClient
from cogo_common.logging import get_logger
from cogo_common.metrics import gauge
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.media import MediaStore
from app.models import AccountStatus, KycAttempt, MediaUpload, User, UserCoverPhoto
from app.service import PRODUCER

log = get_logger(__name__)
DELETED_NAME = "Deleted user"
# Every service that holds data about a user (spec §7)
DELETION_PARTICIPANTS = ("auth", "journey", "social", "chat", "notification")


class AccountDeletion:
    def __init__(self, session: AsyncSession, *, media: MediaStore) -> None:
        self._session = session
        self._media = media

    async def delete(self, user_id: UUID) -> datetime:
        """Idempotent: deleting again returns the original time."""
        now = datetime.now(UTC)
        async with self._session.begin():
            user = await self._session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise NotFound("User not found")
            if user.deleted_at is not None:
                return user.deleted_at
            keys = list(
                (
                    await self._session.scalars(
                        select(MediaUpload.object_key).where(MediaUpload.user_id == user_id)
                    )
                ).all()
            )
            # Spec §7: "Deleted user"; media, bio, tags and KYC fields cleared.
            for field in (
                "username", "dob", "gender", "bio", "profession", "city_name", "city_place_id",
                "city_lat", "city_lng", "avatar_url", "official_email", "email_verified_at",
                "gov_id_verified_at", "gov_id_type", "gov_id_last4", "kyc_review_status",
                "suspended_until", "kyc_prompt_snoozed_until",
            ):  # fmt: skip
                setattr(user, field, None)
            user.name = DELETED_NAME
            user.personality_tag_ids = []
            user.kyc_level = "none"
            user.account_status = AccountStatus.DELETED
            user.deleted_at = now
            user.version += 1
            user.updated_at = now
            for model in (UserCoverPhoto, KycAttempt, MediaUpload):
                await self._session.execute(delete(model).where(model.user_id == user_id))
            enqueue(
                self._session,
                EventEnvelope(
                    type=RoutingKeys.USER_DELETED,
                    producer=PRODUCER,
                    data=UserDeleted(id=user_id, deleted_at=now).model_dump(mode="json"),
                ),
            )
        for key in keys:  # after commit; a leftover object is unreferenced either way
            try:
                await self._media.delete(key)
            except Exception as exc:
                log.warning("deleted_user_media_left", error=type(exc).__name__)
        log.info("account_deleted", user_id=str(user_id), media_objects=len(keys))
        return now


class DeletionTracker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        participants: dict[str, InternalClient],
        *,
        alert_after: timedelta = timedelta(hours=24),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._participants = participants
        self._alert_after = alert_after
        self._alerted: dict[UUID, datetime] = {}
        self.overdue = 0
        gauge(
            "account_deletions_overdue",
            "Account deletions still incomplete after the alert window (24 h)",
            lambda: [(float(self.overdue), {})],
        )

    async def _done(self, name: str, user_id: UUID) -> bool:
        try:
            response = await self._participants[name].request(
                "GET", f"/internal/admin/deletion-status/{user_id}", idempotent=True
            )
            return response.status_code == 200 and bool(response.json()["data"]["done"])
        except Exception as exc:
            log.info("deletion_status_unavailable", service=name, error=type(exc).__name__)
            return False

    async def pending(self, user_id: UUID) -> list[str]:
        names = list(self._participants)
        done = await asyncio.gather(*(self._done(n, user_id) for n in names))
        return [n for n, ok in zip(names, done, strict=True) if not ok]

    async def run_once(self, limit: int = 50) -> int:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(User.id, User.deleted_at)
                    .where(User.deleted_at.is_not(None), User.deletion_completed_at.is_(None))
                    .order_by(User.deleted_at)
                    .limit(limit)
                )
            ).all()
        completed, overdue = 0, 0
        now = datetime.now(UTC)
        for user_id, deleted_at in rows:
            waiting = await self.pending(user_id)
            if not waiting:
                async with self._sessionmaker() as session, session.begin():
                    await session.execute(
                        update(User).where(User.id == user_id).values(deletion_completed_at=now)
                    )
                self._alerted.pop(user_id, None)
                completed += 1
                log.info("account_deletion_completed", user_id=str(user_id))
            elif deleted_at is not None and now - deleted_at > self._alert_after:
                overdue += 1
                last = self._alerted.get(user_id)
                if last is None or now - last > timedelta(hours=1):  # alert hourly, not per run
                    self._alerted[user_id] = now
                    log.error("account_deletion_incomplete", user_id=str(user_id), pending=waiting)
        self.overdue = overdue
        return completed
