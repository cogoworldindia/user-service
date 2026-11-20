"""user-service's queue (spec §1A.4): one durable queue, handlers per event type."""

from cogo_common.events.consumer import upsert_projection
from cogo_common.events.contracts import JourneySummary, SocialFollowed, UserBlocked, UserRegistered
from cogo_common.events.dispatch import EventDispatcher
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import USER_SERVICE, RoutingKeys
from faststream.rabbit import RabbitBroker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BlockProjection
from app.service import UserService


def register_consumers(
    broker: RabbitBroker, sessionmaker: async_sessionmaker[AsyncSession]
) -> EventDispatcher:
    dispatcher = EventDispatcher(sessionmaker)

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
