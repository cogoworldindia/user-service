"""The Cashfree DigiLocker adapter against a stubbed Cashfree API (request shapes, webhook
signature, event mapping, identity extraction), and the provider factory's guards."""

import base64
import hashlib
import hmac
import json
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cogo_common.errors import BadRequest, ServiceUnavailable
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from app.config import Settings
from app.kyc.cashfree import CashfreeDigiLockerProvider, last4, parse_dob
from app.kyc.factory import build_provider

BASE = "https://sandbox.cashfree.com/verification"
SECRET = "cf-test-secret"


class StubCashfree:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.documents: dict[str, tuple[int, dict[str, Any]]] = {
            "AADHAAR": (
                200,
                {
                    "status": "SUCCESS",
                    "name": "Asha Kumari Sharma",
                    "dob": "02-04-1996",
                    "uid": "xxxxxxxx5647",
                    "photo_link": "base64...",
                },
            ),
            "PAN": (
                200,
                {"pan": "ABCPS1234D", "name_pan_card": "ASHA SHARMA", "dob": "02-04-1996"},
            ),
        }
        self.session_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/digilocker"):
            body = json.loads(request.content)
            return httpx.Response(
                self.session_status,
                json={
                    "verification_id": body["verification_id"],
                    "reference_id": 12345,
                    "url": "https://verification.cashfree.com/dgl?shortCode=abc",
                    "status": "PENDING",
                },
            )
        document = request.url.path.rsplit("/", 1)[-1]
        status, body = self.documents[document]
        return httpx.Response(status, json=body)


def provider(stub: StubCashfree, **kwargs: Any) -> CashfreeDigiLockerProvider:
    options: dict[str, Any] = {
        "base_url": BASE,
        "client_id": "CF123",
        "client_secret": SECRET,
        "documents": ["AADHAAR"],
        **kwargs,
    }
    return CashfreeDigiLockerProvider(
        **options, http=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    )


def signed(
    payload: dict[str, Any], timestamp: str = "1746427759733"
) -> tuple[dict[str, str], bytes]:
    raw = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), timestamp.encode() + raw, hashlib.sha256).digest()
    return {
        "x-webhook-timestamp": timestamp,
        "x-webhook-signature": base64.b64encode(digest).decode(),
    }, raw


def event(event_type: str, ref: str, **data: Any) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "event_time": "2026-11-20T10:00:00+05:30",
        "version": "v1",
        "data": {
            "verification_id": ref,
            "reference_id": 12345,
            "status": "AUTHENTICATED",
            "document_requested": ["AADHAAR"],
            "document_consent": None,
            **data,
        },
    }


async def test_session_request_shape() -> None:
    stub = StubCashfree()
    attempt = uuid4()
    session = await provider(stub).start_session(
        attempt_id=attempt, return_url=f"https://cogo.app/kyc/return?attempt_id={attempt}"
    )
    assert session.redirect_url.startswith("https://verification.cashfree.com/")
    assert session.provider_ref == str(attempt)
    [request] = stub.requests
    assert request.url == f"{BASE}/digilocker"
    assert (
        request.headers["x-client-id"] == "CF123" and request.headers["x-client-secret"] == SECRET
    )
    assert "x-cf-signature" not in request.headers
    assert json.loads(request.content) == {
        "verification_id": str(attempt),
        "document_requested": ["AADHAAR"],
        "redirect_url": f"https://cogo.app/kyc/return?attempt_id={attempt}",
        "user_flow": "signup",
    }

    stub.session_status = 401
    with pytest.raises(ServiceUnavailable):
        await provider(stub).start_session(attempt_id=uuid4(), return_url="https://x/y")


async def test_production_signature_header() -> None:
    """x-cf-signature: RSA-OAEP(SHA-1) of "clientId.unixTimestamp", Base64."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    stub = StubCashfree()
    await provider(stub, public_key_pem=pem).start_session(
        attempt_id=uuid4(), return_url="https://x"
    )
    sealed = base64.b64decode(stub.requests[0].headers["x-cf-signature"])
    plain = key.decrypt(
        sealed,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),  # noqa: S303
    ).decode()
    client_id, timestamp = plain.split(".")
    assert client_id == "CF123" and timestamp.isdigit()


def test_webhook_signature() -> None:
    cf = provider(StubCashfree())
    headers, raw = signed(event("DIGILOCKER_VERIFICATION_SUCCESS", "a1"))
    assert cf.verify_webhook(headers, raw)
    assert not cf.verify_webhook(headers, raw.replace(b"a1", b"a2"))  # body changed
    assert not cf.verify_webhook({**headers, "x-webhook-timestamp": "1"}, raw)  # timestamp signed
    assert not cf.verify_webhook({"x-webhook-timestamp": headers["x-webhook-timestamp"]}, raw)


async def test_success_fetches_the_masked_aadhaar() -> None:
    stub = StubCashfree()
    result = await provider(stub).parse_result(
        event("DIGILOCKER_VERIFICATION_SUCCESS", "att-1", document_consent=["AADHAAR"])
    )
    assert result is not None and result.success and result.provider_ref == "att-1"
    assert (result.id_type, result.last4) == ("aadhaar", "5647")
    assert (result.name, result.dob) == ("Asha Kumari Sharma", date(1996, 4, 2))
    assert "Asha" not in repr(result)  # identity data stays out of logs
    fetch = stub.requests[-1]
    assert fetch.url.path == "/verification/digilocker/document/AADHAAR"
    assert fetch.url.params["verification_id"] == "att-1"


async def test_pan_keeps_only_the_last_four() -> None:
    stub = StubCashfree()
    result = await provider(stub, documents=["AADHAAR", "PAN"]).parse_result(
        event("DIGILOCKER_VERIFICATION_SUCCESS", "att-2", document_consent=["PAN"])
    )
    assert result is not None and (result.id_type, result.last4, result.name) == (
        "pan",
        "234D",
        "ASHA SHARMA",
    )


async def test_failure_events_and_unlinked_aadhaar() -> None:
    stub = StubCashfree()
    cf = provider(stub)
    for name in (
        "DIGILOCKER_VERIFICATION_FAILURE",
        "DIGILOCKER_VERIFICATION_LINK_EXPIRED",
        "DIGILOCKER_VERIFICATION_CONSENT_DENIED",
        "DIGILOCKER_VERIFICATION_CONSENT_EXPIRED",  # data.status still says AUTHENTICATED
    ):
        result = await cf.parse_result(event(name, "att-3"))
        assert result is not None and not result.success
    assert await cf.parse_result(event("SOMETHING_NEW", "att-3")) is None
    stub.documents["AADHAAR"] = (200, {"status": "AADHAAR_NOT_LINKED"})
    result = await cf.parse_result(event("DIGILOCKER_VERIFICATION_SUCCESS", "att-3"))
    assert result is not None and not result.success
    assert stub.requests[-1].url.path.endswith("/document/AADHAAR")  # it did ask
    none_consented = await cf.parse_result(
        event("DIGILOCKER_VERIFICATION_SUCCESS", "att-3", document_consent=["PAN"])
    )
    assert none_consented is not None and not none_consented.success
    with pytest.raises(BadRequest):
        await cf.parse_result({"event_type": "DIGILOCKER_VERIFICATION_SUCCESS", "data": {}})


async def test_document_fetch_failure_is_retryable() -> None:
    stub = StubCashfree()
    stub.documents["AADHAAR"] = (500, {})
    with pytest.raises(ServiceUnavailable):  # -> 503 to Cashfree, which redelivers
        await provider(stub).parse_result(event("DIGILOCKER_VERIFICATION_SUCCESS", "att-4"))
    stub.documents["AADHAAR"] = (403, {"code": "consent_not_granted"})
    refused = await provider(stub).parse_result(event("DIGILOCKER_VERIFICATION_SUCCESS", "att-4"))
    assert refused is not None and not refused.success  # a refusal is final, not retried


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("02-04-1996", date(1996, 4, 2)),
        ("1996-04-02", date(1996, 4, 2)),
        ("garbage", None),
        (None, None),
    ],
)
def test_dob_formats(raw: str | None, parsed: date | None) -> None:
    assert parse_dob(raw) == parsed


def test_last4() -> None:
    assert last4("xxxxxxxx5647") == "5647"
    assert last4("DL-04 2011 0012345") == "2345"
    assert last4(None) is None


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://x/y",
        RABBITMQ_URL="amqp://x",
        KYC_OTP_HMAC_KEY=SecretStr("k"),
        **kwargs,
    )


def test_factory_guards(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="development"):
        build_provider(_settings(ENV="production", KYC_MOCK_WEBHOOK_SECRET=SecretStr("s")))
    with pytest.raises(RuntimeError, match="KYC_MOCK_WEBHOOK_SECRET"):
        build_provider(_settings(ENV="development"))
    with pytest.raises(RuntimeError, match="CLIENT_ID"):
        build_provider(_settings(KYC_PROVIDER="cashfree"))
    with pytest.raises(RuntimeError, match="https"):
        build_provider(
            _settings(
                KYC_PROVIDER="cashfree",
                KYC_CASHFREE_CLIENT_ID="CF",
                KYC_CASHFREE_CLIENT_SECRET=SecretStr("s"),
            )
        )
    cf = build_provider(
        _settings(
            KYC_PROVIDER="cashfree",
            KYC_CASHFREE_CLIENT_ID="CF",
            KYC_CASHFREE_CLIENT_SECRET=SecretStr("s"),
            KYC_RETURN_URL="https://cogo.app/kyc/return",
        )
    )
    assert cf.name == "cashfree"
    with pytest.raises(ValueError, match="KYC_CASHFREE_DOCUMENTS"):
        CashfreeDigiLockerProvider(
            base_url=BASE, client_id="c", client_secret="s", documents=["PASSPORT"]
        )
