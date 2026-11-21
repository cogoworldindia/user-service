"""Image moderation for avatars and covers (spec §5.10, §9): Cloud Vision SafeSearch in
production, a mock in development and tests. Selected by IMAGE_MODERATION_PROVIDER."""

import base64
from typing import Protocol

import httpx
from cogo_common.errors import ServiceUnavailable
from cogo_common.logging import get_logger

log = get_logger(__name__)

# Vision likelihoods, weakest to strongest
LIKELIHOODS = ("UNKNOWN", "VERY_UNLIKELY", "UNLIKELY", "POSSIBLE", "LIKELY", "VERY_LIKELY")
FLAG_CATEGORIES = ("adult", "violence")
FLAG_AT = LIKELIHOODS.index("LIKELY")
MOCK_FLAG_MARKER = b"cogo-test:flag"  # dev/test: an image containing these bytes is flagged


def is_flagged(labels: dict[str, str]) -> bool:
    """Spec §5.10: adult or violence at LIKELY or above."""
    return any(
        LIKELIHOODS.index(labels.get(c, "UNKNOWN")) >= FLAG_AT
        for c in FLAG_CATEGORIES
        if labels.get(c, "UNKNOWN") in LIKELIHOODS
    )


class ImageModerator(Protocol):
    async def labels(self, image: bytes) -> dict[str, str]: ...

    async def aclose(self) -> None: ...


class MockImageModerator:
    async def labels(self, image: bytes) -> dict[str, str]:
        flagged = MOCK_FLAG_MARKER in image
        return {"adult": "VERY_LIKELY" if flagged else "VERY_UNLIKELY", "violence": "VERY_UNLIKELY"}

    async def aclose(self) -> None:
        return None


class GoogleVisionModerator:
    """`images:annotate` with SAFE_SEARCH_DETECTION, image bytes inline (≤ 5 MB uploads)."""

    URL = "https://vision.googleapis.com/v1/images:annotate"

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))

    async def labels(self, image: bytes) -> dict[str, str]:
        body = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(image).decode()},
                    "features": [{"type": "SAFE_SEARCH_DETECTION"}],
                }
            ]
        }
        try:
            response = await self._http.post(self.URL, params={"key": self._key}, json=body)
        except httpx.HTTPError as exc:
            raise ServiceUnavailable("Image moderation unavailable") from exc
        if response.status_code != 200:
            log.error("vision_failed", status=response.status_code)
            raise ServiceUnavailable("Image moderation unavailable")
        result = (response.json().get("responses") or [{}])[0]
        if "error" in result:  # e.g. an unreadable image
            log.warning("vision_image_error", code=result["error"].get("code"))
            return {}
        annotation = result.get("safeSearchAnnotation") or {}
        return {k: str(v) for k, v in annotation.items() if isinstance(v, str)}

    async def aclose(self) -> None:
        await self._http.aclose()
