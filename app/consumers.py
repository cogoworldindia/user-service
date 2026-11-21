"""user-service's queue (spec §1A.4): one durable queue, handlers per event type."""

from cogo_common.events.consumer import upsert_projection
from cogo_common.events.contracts import (
    ContentModeration,
    JourneySummary,
    MediaUploaded,
    SocialFollowed,
    UserBlocked,
    UserRegistered,
)
from cogo_common.events.dispatch import EventDispatcher
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import USER_SERVICE, RoutingKeys
from cogo_common.logging import get_logger
from faststream.rabbit import RabbitBroker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import take_down_media
from app.media import MediaStore
from app.models import BlockProjection, MediaStatus, MediaUpload
from app.moderation import ImageModerator, is_flagged
from app.service import UserService

log = get_logger(__name__)


def register_consumers(
    broker: RabbitBroker,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    media: MediaStore,
    moderator: ImageModerator,
) -> EventDispatcher:
    dispatcher = EventDispatcher(sessionmaker)

    @dispatcher.on(RoutingKeys.MEDIA_UPLOADED)
    async def on_uploaded(event: EventEnvelope, session: AsyncSession) -> None:
        """Image moderation (spec §5.10). A provider outage raises, so the event is retried
        and finally dead-lettered (alert); the photo stays up meanwhile."""
        uploaded = MediaUploaded.model_validate(event.data)
        upload = await session.get(MediaUpload, uploaded.media_id, with_for_update=True)
        if upload is None or upload.status != MediaStatus.READY.value:
            return  # deleted, or already taken down
        try:
            image = await media.get(upload.object_key)
        except Exception as exc:  # the object is gone (deleted account): nothing to check
            log.info("moderation_object_missing", error=type(exc).__name__)
            return
        labels = await moderator.labels(image)
        if is_flagged(labels):
            await take_down_media(session, upload, media=media, labels=labels)

    @dispatcher.on(RoutingKeys.SAFETY_CONTENT_HIDDEN, RoutingKeys.SAFETY_CONTENT_UNHIDDEN)
    async def on_moderation(event: EventEnvelope, session: AsyncSession) -> None:
        """Reported photos (target_type media) hidden at the threshold, or restored by an
        admin's dismiss. Other target types belong to other services."""
        target = ContentModeration.model_validate(event.data)
        if target.target_type != "media":
            return
        upload = await session.get(MediaUpload, target.target_id, with_for_update=True)
        if upload is None:
            return
        if event.type == RoutingKeys.SAFETY_CONTENT_HIDDEN:
            if upload.status != MediaStatus.FLAGGED.value:
                await take_down_media(session, upload, media=media)
        elif upload.status == MediaStatus.FLAGGED.value:
            upload.status = MediaStatus.READY.value  # usable again; not re-applied for the user

    @dispatcher.on(RoutingKeys.USER_REGISTERED)
    async def on_registered(event: EventEnvelope, session: AsyncSession) -> None:
        await UserService(session).ensure_user(UserRegistered.model_validate(event.data).user_id)

    @dispatcher.on(RoutingKeys.SOCIAL_FOLLOWED, RoutingKeys.SOCIAL_UNFOLLOWED)
    async def on_follow(event: EventEnvelope, session: AsyncSession) -> None:
        payload = SocialFollowed.model_validate(event.data)
        delta = 1 if event.type == RoutingKeys.SOCIAL_FOLLOWED else -1
        service = UserService(session)
        await service.adjust_counter(payload.follower_id, "following_count", delta)
        await service.adjust_counter(payload.followee_id, "followers_count", delta)

    @dispatcher.on(RoutingKeys.JOURNEY_CREATED, RoutingKeys.JOURNEY_DELETED)
    async def on_journey(event: EventEnvelope, session: AsyncSession) -> None:
        journey = JourneySummary.model_validate(event.data)
        delta = 1 if event.type == RoutingKeys.JOURNEY_CREATED else -1
        await UserService(session).adjust_counter(journey.owner_id, "journeys_count", delta)

    @dispatcher.on(RoutingKeys.SOCIAL_USER_BLOCKED, RoutingKeys.SOCIAL_USER_UNBLOCKED)
    async def on_block(event: EventEnvelope, session: AsyncSession) -> None:
        block = UserBlocked.model_validate(event.data)
        await upsert_projection(
            session,
            BlockProjection,
            {
                "blocker_id": block.blocker_id,
                "blocked_id": block.blocked_id,
                "active": event.type == RoutingKeys.SOCIAL_USER_BLOCKED,
                "source_version": block.version,
            },
            key=("blocker_id", "blocked_id"),
        )

    dispatcher.subscribe(broker, USER_SERVICE)
    return dispatcher
