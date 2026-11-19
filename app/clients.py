"""Calls to other services over the internal API (service tokens, timeouts, breaker)."""

from dataclasses import dataclass
from uuid import UUID

from cogo_common.errors import BadRequest
from cogo_common.internal_http import InternalClient, UpstreamUnavailable
from cogo_common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Place:
    place_id: str
    name: str
    region: str | None
    lat: float
    lng: float


class PlacesClient:
    """journey-service owns the Places proxy (spec §9); user-service resolves cities with it."""

    def __init__(self, client: InternalClient) -> None:
        self._client = client

    async def resolve(self, place_id: str) -> Place:
        response = await self._client.request(
            "GET", f"/internal/places/{place_id}", idempotent=True
        )
        if response.status_code == 404:
            raise BadRequest("Unknown place", error_type="invalid_place")
        if response.status_code != 200:
            raise UpstreamUnavailable("Places unavailable")
        data = response.json()["data"]
        return Place(data["place_id"], data["name"], data.get("region"), data["lat"], data["lng"])


class SocialClient:
    """Display-only relation flags (spec §1A.3 #2): one batched call, 150 ms budget,
    `None` on any failure so the profile still renders."""

    def __init__(self, client: InternalClient) -> None:
        self._client = client

    async def follow_status(self, viewer_id: UUID, user_ids: list[UUID]) -> dict[UUID, bool] | None:
        try:
            response = await self._client.request(
                "POST",
                "/internal/relations/follow-status",
                json={"viewer_id": str(viewer_id), "user_ids": [str(u) for u in user_ids]},
                idempotent=True,
                read_timeout=0.15,
            )
            if response.status_code != 200:
                return None
            return {UUID(k): bool(v) for k, v in response.json()["data"].items()}
        except Exception as exc:
            log.info("follow_status_degraded", error=type(exc).__name__)
            return None
