from datetime import UTC, datetime, timedelta
from typing import Any

from cogo_common.context import get_request_id
from cogo_common.errors import ServiceUnavailable
from cogo_common.events.contracts import EmailRequested
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import EVENTS_EXCHANGE, RoutingKeys
from cogo_common.logging import get_logger
from faststream.rabbit import RabbitBroker

log = get_logger(__name__)


class KycMailer:
    """Hands the official-email code to notification-service (spec §9 email), like
    auth-service does for sign-in codes. Published directly rather than through the outbox,
    so the plaintext code is never written to a table; if queuing fails the caller gets a
    503 and can retry."""

    def __init__(self, broker: RabbitBroker, *, producer: str) -> None:
        self._broker = broker
        self._producer = producer

    async def send_code(self, email: str, code: str, *, ttl_seconds: int) -> None:
        request = EmailRequested(
            template="kyc_email_otp",
            to=email,
            params={"code": code, "ttl_minutes": max(1, ttl_seconds // 60)},
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        event = EventEnvelope(
            type=RoutingKeys.EMAIL_REQUESTED,
            producer=self._producer,
            data=request.model_dump(mode="json"),
        )
        headers: dict[str, Any] = {"x-request-id": rid} if (rid := get_request_id()) else {}
        try:
            await self._broker.publish(
                event.model_dump(mode="json"),
                exchange=EVENTS_EXCHANGE,
                routing_key=RoutingKeys.EMAIL_REQUESTED,
                message_id=str(event.id),
                headers=headers,
                content_type="application/json",
                persist=True,
                mandatory=True,
            )
        except Exception as exc:
            log.error("kyc_email_enqueue_failed", error=type(exc).__name__)
            raise ServiceUnavailable("Could not send the code right now, please retry") from exc
