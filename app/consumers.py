"""user-service's queue (spec §1A.4): one durable queue, handlers per event type."""

from cogo_common.events.contracts import JourneySummary, SocialFollowed, UserRegistered
from cogo_common.events.dispatch import EventDispatcher
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import USER_SERVICE, RoutingKeys
from faststream.rabbit import RabbitBroker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    dispatcher.subscribe(broker, USER_SERVICE)
    return dispatcher
