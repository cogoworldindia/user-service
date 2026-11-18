import asyncio

from cogo_common.events.contracts import UserRegistered
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import EVENTS_EXCHANGE, USER_SERVICE_USER_REGISTERED
from cogo_common.logging import get_logger
from faststream import AckPolicy
from faststream.exceptions import RejectMessage
from faststream.rabbit import RabbitBroker
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.service import UserService

log = get_logger(__name__)


def register_consumers(
    broker: RabbitBroker, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    @broker.subscriber(
        USER_SERVICE_USER_REGISTERED.queue(), EVENTS_EXCHANGE, ack_policy=AckPolicy.NACK_ON_ERROR
    )
    async def on_user_registered(event: EventEnvelope) -> None:
        try:
            payload = UserRegistered.model_validate(event.data)
        except ValidationError:
            log.error("user_registered_invalid_payload", event_id=str(event.id))
            raise RejectMessage() from None  # poison message -> DLQ immediately

        delay = 0.5
        for attempt in range(3):
            try:
                async with sessionmaker() as session:
                    created = await UserService(session).handle_registered(
                        event.id, payload.user_id
                    )
                log.info("user_registered_processed", user_id=str(payload.user_id), new=created)
                return
            except Exception:
                if attempt == 2:
                    raise  # nack -> redelivered; dead-lettered after the queue delivery limit
                log.warning("user_registered_retry", attempt=attempt + 1)
                await asyncio.sleep(delay)
                delay *= 2
