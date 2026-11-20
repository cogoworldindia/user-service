"""Cashfree Secure ID, DigiLocker flow: the production aggregator (spec §9 eKYC provider).

Built from Cashfree's public API reference (docs/RECON.md D38 lists the sources and the
points the docs leave open). Verify against a sandbox delivery before go-live.

Flow:
1. `POST /digilocker` with our attempt id as `verification_id` -> a hosted `url` that the
   app opens in an in-app browser (valid 10 minutes).
2. Cashfree calls our webhook. The body is signed: Base64(HMAC-SHA256(client_secret,
   x-webhook-timestamp + raw body)) in `x-webhook-signature`.
3. On `DIGILOCKER_VERIFICATION_SUCCESS` the identity is fetched from
   `GET /digilocker/document/{type}`. Aadhaar comes back masked (`xxxxxxxx5647`); PAN and
   driving-licence numbers come back in full, and only their last 4 digits are kept.
   Consent lasts an hour, so this happens right away, inside the webhook. A failed fetch
   returns 503 and Cashfree redelivers.
"""

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

import httpx
from cogo_common.errors import BadRequest, ServiceUnavailable
from cogo_common.logging import get_logger
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.kyc.providers import KycResult, KycSession

log = get_logger(__name__)

SUCCESS = "DIGILOCKER_VERIFICATION_SUCCESS"
FAILURES = frozenset(
    {
        "DIGILOCKER_VERIFICATION_FAILURE",
        "DIGILOCKER_VERIFICATION_LINK_EXPIRED",
        "DIGILOCKER_VERIFICATION_CONSENT_DENIED",
        # Sent an hour after consent. Its data.status still says AUTHENTICATED, so branch on
        # the event type; if SUCCESS was already applied this is a no-op (attempt is final).
        "DIGILOCKER_VERIFICATION_CONSENT_EXPIRED",
    }
)
# document type -> (id_type we store, name field, number field)
_DOCUMENTS: dict[str, tuple[str, str, str]] = {
    "AADHAAR": ("aadhaar", "name", "uid"),
    "PAN": ("pan", "name_pan_card", "pan"),
    "DRIVING_LICENSE": ("driving_licence", "name", "dl_number"),
}
_DOB_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


def parse_dob(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def last4(number: Any) -> str | None:
    """Last 4 characters of an ID number, separators dropped ("xxxxxxxx5647" -> "5647")."""
    chars = "".join(c for c in str(number or "") if c.isalnum())
    return chars[-4:] or None


class CashfreeDigiLockerProvider:
    name = "cashfree"

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        documents: list[str],
        public_key_pem: bytes | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        unknown = set(documents) - set(_DOCUMENTS)
        if not documents or unknown:
            raise ValueError(f"KYC_CASHFREE_DOCUMENTS must be from {sorted(_DOCUMENTS)}")
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._secret = client_secret.encode()
        self._documents = documents
        self._public_key: rsa.RSAPublicKey | None = None
        if public_key_pem:  # production "2FA" alternative to IP allow-listing
            key = serialization.load_pem_public_key(public_key_pem)
            if not isinstance(key, rsa.RSAPublicKey):
                raise ValueError("the Cashfree public key must be RSA")
            self._public_key = key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))

    def _headers(self) -> dict[str, str]:
        headers = {"x-client-id": self._client_id, "x-client-secret": self._secret.decode()}
        if self._public_key is not None:
            plain = f"{self._client_id}.{int(time.time())}".encode()
            # RSA-OAEP with SHA-1 is what Cashfree's x-cf-signature scheme specifies.
            oaep = padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),  # noqa: S303
                algorithm=hashes.SHA1(),  # noqa: S303
                label=None,
            )
            sealed = self._public_key.encrypt(plain, oaep)
            headers["x-cf-signature"] = base64.b64encode(sealed).decode()
        return headers

    async def start_session(self, *, attempt_id: UUID, return_url: str) -> KycSession:
        body = {
            "verification_id": str(attempt_id),
            "document_requested": self._documents,
            "redirect_url": return_url,
            "user_flow": "signup",
        }
        try:
            response = await self._http.post(
                f"{self._base}/digilocker", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(
                "ID verification is unavailable right now, please retry",
                error_type="kyc_provider_unavailable",
            ) from exc
        if response.status_code >= 400:
            log.error("cashfree_session_failed", status=response.status_code)
            raise ServiceUnavailable(
                "ID verification is unavailable right now, please retry",
                error_type="kyc_provider_unavailable",
            )
        data = response.json()
        return KycSession(redirect_url=str(data["url"]), provider_ref=str(data["verification_id"]))

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        timestamp = headers.get("x-webhook-timestamp", "")
        signature = headers.get("x-webhook-signature", "")
        if not timestamp or not signature:
            return False
        digest = hmac.new(self._secret, timestamp.encode() + body, hashlib.sha256).digest()
        return hmac.compare_digest(signature, base64.b64encode(digest).decode())

    async def parse_result(self, payload: Mapping[str, Any]) -> KycResult | None:
        event = payload.get("event_type")
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("verification_id"):
            raise BadRequest(
                "Malformed provider result",
                error_type="invalid_payload",
                details="data.verification_id",
            )
        ref = str(data["verification_id"])
        if event == SUCCESS:
            return await self._identity(ref, data)
        if event in FAILURES:
            return KycResult(provider_ref=ref, success=False)
        log.info("cashfree_event_ignored", event_type=str(event)[:64])
        return None

    async def _identity(self, ref: str, data: Mapping[str, Any]) -> KycResult:
        # document_consent can be null in the payload; then try what we asked for
        consented = (
            data.get("document_consent") or data.get("document_requested") or self._documents
        )
        document = next((d for d in self._documents if d in consented), None)
        if document is None:
            return KycResult(provider_ref=ref, success=False)
        id_type, name_field, number_field = _DOCUMENTS[document]
        try:
            response = await self._http.get(
                f"{self._base}/digilocker/document/{document}",
                params={"verification_id": ref},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(
                "Provider unavailable", error_type="kyc_provider_unavailable"
            ) from exc
        if response.status_code >= 500:  # transient: 503 makes Cashfree redeliver
            log.error("cashfree_document_failed", status=response.status_code, document=document)
            raise ServiceUnavailable("Provider unavailable", error_type="kyc_provider_unavailable")
        if response.status_code >= 400:  # e.g. consent_not_granted: this check failed
            log.warning("cashfree_document_refused", status=response.status_code, document=document)
            return KycResult(provider_ref=ref, success=False)
        doc = response.json()
        if doc.get("status") not in (None, "SUCCESS"):  # e.g. AADHAAR_NOT_LINKED
            return KycResult(provider_ref=ref, success=False)
        return KycResult(
            provider_ref=ref,
            success=True,
            id_type=id_type,
            last4=last4(doc.get(number_field)),
            name=doc.get(name_field),
            dob=parse_dob(doc.get("dob")),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
