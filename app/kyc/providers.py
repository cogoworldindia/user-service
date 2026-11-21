"""Government-ID verification providers (spec §9): one interface, a mock for development
and tests, and the aggregator used in production. Selected by `KYC_PROVIDER`."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from cogo_common.errors import BadRequest


@dataclass(frozen=True, slots=True)
class KycSession:
    redirect_url: str  # the provider's hosted flow, opened in an in-app browser
    provider_ref: str


@dataclass(frozen=True, slots=True, repr=False)
class KycResult:
    """What a finished provider session tells us. `name` and `dob` are only compared with
    the profile and then dropped: they're never stored, logged or put in events."""

    provider_ref: str
    success: bool
    id_type: str | None = None
    last4: str | None = None
    name: str | None = None
    dob: date | None = None

    def __repr__(self) -> str:  # keep identity data out of logs and tracebacks
        return f"KycResult(provider_ref={self.provider_ref!r}, success={self.success})"


class KycProvider(Protocol):
    name: str

    async def start_session(self, *, attempt_id: UUID, return_url: str) -> KycSession: ...

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    async def parse_result(self, payload: Mapping[str, Any]) -> KycResult | None:
        """None: an event with nothing to apply (acknowledged, not retried)."""
        ...

    async def aclose(self) -> None: ...


def _invalid(detail: str) -> BadRequest:
    return BadRequest("Malformed provider result", error_type="invalid_payload", details=detail)


class MockKycProvider:
    """Development stand-in for the aggregator (spec §1A.7). Its "hosted flow" is a page
    served by user-service itself (`app.kyc.mock_pages`): the tester picks an outcome there
    (verify / name mismatch / fail), and the mock then calls the real webhook with a signed
    payload, the way a provider would. Refused in production."""

    name = "mock"
    SIGNATURE_HEADER = "x-mock-signature"

    def __init__(self, *, secret: bytes, public_base_url: str) -> None:
        self._secret = secret
        self._base = public_base_url.rstrip("/")

    def sign(self, data: bytes) -> str:
        return hmac.new(self._secret, data, hashlib.sha256).hexdigest()

    def page_signature(self, provider_ref: str) -> str:
        return self.sign(b"page:" + provider_ref.encode())

    async def start_session(self, *, attempt_id: UUID, return_url: str) -> KycSession:
        ref = f"mock_{attempt_id.hex}"
        url = f"{self._base}/v1/kyc/mock/{ref}?sig={self.page_signature(ref)}"
        return KycSession(redirect_url=url, provider_ref=ref)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        signature = headers.get(self.SIGNATURE_HEADER, "")
        # bytes: compare_digest raises on a str with non-ASCII characters (a 500, found by fuzzing)
        return bool(signature) and hmac.compare_digest(signature.encode(), self.sign(body).encode())

    async def parse_result(self, payload: Mapping[str, Any]) -> KycResult | None:
        try:
            ref, status = str(payload["ref"]), payload["status"]
            dob = date.fromisoformat(payload["dob"]) if payload.get("dob") else None
        except (KeyError, TypeError, ValueError):
            raise _invalid("ref, status and an ISO dob are required") from None
        if status not in ("success", "failed"):
            raise _invalid("status must be success or failed")
        return KycResult(
            provider_ref=ref,
            success=status == "success",
            id_type=payload.get("id_type"),
            last4=payload.get("last4"),
            name=payload.get("name"),
            dob=dob,
        )

    async def aclose(self) -> None:
        return None
