import re
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from cogo_common.errors import BadRequest, Conflict, Forbidden, NotFound
from cogo_common.events.contracts import CitySnapshot, MediaUploaded, UserSnapshot
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.outbox import enqueue
from cogo_common.events.topology import RoutingKeys
from cogo_common.ids import new_id
from cogo_common.scoring import age_years
from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients import Place
from app.media import CONTENT_TYPES, MediaStore
from app.models import (
    AccountStatus,
    BlockProjection,
    MediaStatus,
    MediaUpload,
    PersonalityTag,
    User,
    UserCoverPhoto,
)

PRODUCER = "user-service"


def age_of(dob: date | None) -> int | None:
    return age_years(dob, datetime.now(UTC).date()) if dob else None


def snapshot(user: User) -> UserSnapshot:
    city = None
    if user.city_name and user.city_lat is not None and user.city_lng is not None:
        city = CitySnapshot(name=user.city_name, lat=user.city_lat, lng=user.city_lng)
    return UserSnapshot(
        id=user.id,
        username=user.username,
        name=user.name,
        dob=user.dob,
        gender=user.gender.value if user.gender else None,
        avatar_url=user.avatar_url,
        city=city,
        personality_tag_ids=list(user.personality_tag_ids or []),
        kyc_level=user.kyc_level,
        account_status=user.account_status.value,
        version=user.version,
    )


def _username_from(name: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:12] or "traveller"
    return f"{base}{secrets.randbelow(9000) + 1000}"


class UserService:
    def __init__(self, session: AsyncSession, *, media: MediaStore | None = None) -> None:
        self._session = session
        self._media = media

    # ------------------------------------------------------------ helpers (inside a tx)
    async def ensure_user(self, user_id: UUID) -> bool:
        """Create the row if missing (idempotent). Emits user.created when it was created."""
        created = (
            await self._session.execute(
                insert(User)
                .values(id=user_id)
                .on_conflict_do_nothing(index_elements=[User.id])
                .returning(User.id)
            )
        ).first() is not None
        if created:
            user = await self._load(user_id)
            assert user is not None
            self._emit(RoutingKeys.USER_CREATED, user)
        return created

    async def _load(self, user_id: UUID, *, lock: bool = False) -> User | None:
        stmt = (
            select(User)
            .options(selectinload(User.covers))
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return await self._session.scalar(stmt)

    def _emit(self, routing_key: str, user: User) -> None:
        enqueue(
            self._session,
            EventEnvelope(
                type=routing_key, producer=PRODUCER, data=snapshot(user).model_dump(mode="json")
            ),
        )

    async def adjust_counter(self, user_id: UUID, column: str, delta: int) -> None:
        col = getattr(User, column)
        await self.ensure_user(user_id)
        await self._session.execute(
            update(User).where(User.id == user_id).values({column: func.greatest(0, col + delta)})
        )

    # ------------------------------------------------------------ reads
    async def get_me(self, user_id: UUID) -> User:
        async with self._session.begin():
            await self.ensure_user(user_id)
            user = await self._load(user_id)
        assert user is not None
        if user.account_status == AccountStatus.DELETED:
            raise NotFound("User not found")
        return user

    async def get_public(self, user_id: UUID, viewer_id: UUID | None = None) -> User:
        user = await self._load(user_id)
        if user is None or user.account_status != AccountStatus.ACTIVE:
            raise NotFound("User not found")
        if (
            viewer_id is not None
            and viewer_id != user_id
            and await self._session.scalar(
                select(
                    exists().where(
                        BlockProjection.active.is_(True),
                        or_(
                            and_(
                                BlockProjection.blocker_id == viewer_id,
                                BlockProjection.blocked_id == user_id,
                            ),
                            and_(
                                BlockProjection.blocker_id == user_id,
                                BlockProjection.blocked_id == viewer_id,
                            ),
                        ),
                    )
                )
            )
        ):
            raise NotFound("This profile isn't available", error_type="profile_unavailable")
        return user

    async def tags(self, ids: list[int] | None = None) -> list[PersonalityTag]:
        stmt = (
            select(PersonalityTag)
            .where(PersonalityTag.active.is_(True))
            .order_by(PersonalityTag.sort_order)
        )
        if ids is not None:
            if not ids:
                return []
            stmt = stmt.where(PersonalityTag.id.in_(ids))
        return list((await self._session.scalars(stmt)).all())

    # ------------------------------------------------------------ writes
    async def update_me(
        self, user_id: UUID, changes: dict[str, Any], *, place: Place | None = None
    ) -> User:
        if not changes:
            raise BadRequest("No profile fields to update", error_type="empty_update")
        try:
            async with self._session.begin():
                await self.ensure_user(user_id)
                user = await self._load(user_id, lock=True)
                assert user is not None
                if user.account_status != AccountStatus.ACTIVE:
                    raise Forbidden("Account is not active", error_type="account_inactive")
                if user.identity_locked and any(
                    field in changes and changes[field] != getattr(user, field)
                    for field in ("name", "dob")
                ):  # spec §5.9: verified name and DOB are read-only
                    raise Forbidden(
                        "Your name and date of birth are verified and can't be changed",
                        error_type="identity_locked",
                    )
                await self._apply(user, changes, place)
                if user.username is None and user.name:
                    user.username = await self._free_username(user.name)
                user.version += 1
                user.updated_at = datetime.now(UTC)
                await self._session.flush()
                self._emit(RoutingKeys.USER_UPDATED, user)
        except IntegrityError as exc:
            if "uq_users_username" in str(exc.orig):
                raise Conflict("Username is taken", error_type="username_taken") from None
            raise
        user = await self._load(user_id)
        assert user is not None
        return user

    async def _apply(self, user: User, changes: dict[str, Any], place: Place | None) -> None:
        for field in ("username", "name", "dob", "gender", "bio", "profession"):
            if field in changes:
                setattr(user, field, changes[field])
        if "personality_tag_ids" in changes:
            ids = changes["personality_tag_ids"] or []
            if len(await self.tags(ids)) != len(ids):
                raise BadRequest("Unknown personality tag", error_type="invalid_tags")
            user.personality_tag_ids = ids
        if "city_place_id" in changes:
            if changes["city_place_id"] is None:
                user.city_name = user.city_place_id = user.city_lat = user.city_lng = None
            else:
                assert place is not None
                user.city_name, user.city_place_id = place.name, place.place_id
                user.city_lat, user.city_lng = place.lat, place.lng
        if "avatar_url" in changes:
            url = changes["avatar_url"]
            if url is not None:
                await self._require_own_media(user.id, url, "avatar")
            user.avatar_url = url

    async def _free_username(self, name: str) -> str:
        for _ in range(5):
            candidate = _username_from(name)
            if not await self._session.scalar(select(User.id).where(User.username == candidate)):
                return candidate
        return f"traveller{new_id().hex[:8]}"

    async def _require_own_media(self, user_id: UUID, url: str, kind: str) -> None:
        """Only accept files this user uploaded and confirmed (spec §9 object storage)."""
        assert self._media is not None
        prefix = f"{self._media.public_base}/"
        key = url[len(prefix) :] if url.startswith(prefix) else None
        ok = (
            key is not None
            and key.startswith(f"users/{user_id}/{kind}/")
            and await self._session.scalar(
                select(MediaUpload.id).where(
                    MediaUpload.object_key == key, MediaUpload.status == MediaStatus.READY.value
                )
            )
        )
        if not ok:
            raise BadRequest("Upload the image first", error_type="invalid_media")

    async def set_covers(self, user_id: UUID, covers: list[tuple[int, str]]) -> User:
        if len({p for p, _ in covers}) != len(covers):
            raise BadRequest("Duplicate cover position", error_type="invalid_covers")
        async with self._session.begin():
            await self.ensure_user(user_id)
            for _, url in covers:
                await self._require_own_media(user_id, url, "cover")
            await self._session.execute(
                delete(UserCoverPhoto).where(UserCoverPhoto.user_id == user_id)
            )
            for position, url in covers:
                self._session.add(
                    UserCoverPhoto(id=new_id(), user_id=user_id, position=position, url=url)
                )
        user = await self._load(user_id)
        assert user is not None
        return user

    async def mark_guide_seen(self, user_id: UUID) -> None:
        async with self._session.begin():
            await self.ensure_user(user_id)
            await self._session.execute(
                update(User)
                .where(User.id == user_id, User.guide_seen_at.is_(None))
                .values(guide_seen_at=func.now())
            )

    # ------------------------------------------------------------ media
    async def create_upload(
        self, user_id: UUID, kind: str, content_type: str
    ) -> tuple[MediaUpload, str, datetime]:
        assert self._media is not None
        media_id = new_id()
        key = f"users/{user_id}/{kind}/{media_id}.{CONTENT_TYPES[content_type]}"
        async with self._session.begin():
            await self.ensure_user(user_id)
            upload = MediaUpload(
                id=media_id, user_id=user_id, kind=kind, object_key=key, content_type=content_type
            )
            self._session.add(upload)
        expires_at = datetime.now(UTC) + timedelta(seconds=600)
        return upload, self._media.presign_put(key, content_type), expires_at

    async def confirm_upload(self, user_id: UUID, media_id: UUID) -> MediaUpload:
        assert self._media is not None
        async with self._session.begin():
            upload = await self._session.scalar(
                select(MediaUpload)
                .where(MediaUpload.id == media_id, MediaUpload.user_id == user_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound("Upload not found")
            if upload.status == MediaStatus.READY.value:
                return upload
            head = await self._media.head(upload.object_key)
            if head is None:
                raise BadRequest("File not uploaded yet", error_type="upload_missing")
            size, content_type = head
            if size > self._media.max_bytes or content_type != upload.content_type:
                await self._media.delete(upload.object_key)
                raise BadRequest("File too large or wrong type", error_type="invalid_upload")
            # Usable now; image moderation (spec §5.10) runs off the request path and takes
            # it down if SafeSearch flags it.
            upload.status = MediaStatus.READY.value
            enqueue(
                self._session,
                EventEnvelope(
                    type=RoutingKeys.MEDIA_UPLOADED,
                    producer=PRODUCER,
                    data=MediaUploaded(
                        media_id=upload.id,
                        owner_id=upload.user_id,
                        kind=cast(Literal["avatar", "cover"], upload.kind),
                        object_key=upload.object_key,
                    ).model_dump(mode="json"),
                ),
            )
        return upload
