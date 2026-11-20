"""The mock provider's "hosted flow" (development and tests only; mounted only when
KYC_PROVIDER=mock, which production refuses).

`redirect_url` from `/v1/me/kyc/gov-id/start` opens this page. The tester picks an outcome;
the mock then calls the real webhook with a signed payload, exactly as a provider's server
would, and sends the browser back to the app's deep link."""

import hmac
import html
import json
from typing import Annotated
from urllib.parse import parse_qs

from cogo_common.errors import BadGateway, BadRequest, NotFound
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.kyc.api import Kyc
from app.kyc.providers import MockKycProvider
from app.models import KycAttemptStatus

router = APIRouter(prefix="/v1/kyc/mock", tags=["kyc (development mock)"])

OUTCOMES = {
    "verified": "ID matches the profile",
    "mismatch": "ID name doesn't match the profile",
    "failed": "Verification fails",
}
MISMATCH_NAME = "Ramesh Iyer"


def _provider(request: Request, ref: str, sig: str) -> MockKycProvider:
    provider = request.app.state.kyc_provider
    if not isinstance(provider, MockKycProvider) or not hmac.compare_digest(
        sig, provider.page_signature(ref)
    ):
        raise NotFound("Session not found")
    return provider


@router.get("/{ref}", response_class=HTMLResponse)
async def mock_page(
    ref: str, request: Request, kyc: Kyc, sig: Annotated[str, Query(max_length=128)] = ""
) -> HTMLResponse:
    provider = _provider(request, ref, sig)
    attempt, _ = await kyc.attempt_for_page(provider.name, ref)
    if attempt.status != KycAttemptStatus.STARTED.value:
        body = "<p>This verification session has already finished.</p>"
    else:
        buttons = "".join(
            f'<button name="outcome" value="{k}">{html.escape(v)}</button>'
            for k, v in OUTCOMES.items()
        )
        body = (
            f'<form method="post" action="/v1/kyc/mock/{html.escape(ref)}">'
            f'<input type="hidden" name="sig" value="{html.escape(sig)}">{buttons}</form>'
        )
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "<title>Test ID verification</title><style>body{font:16px system-ui;margin:24px}"
        "button{display:block;width:100%;margin:8px 0;padding:14px;font-size:16px}</style>"
        "</head><body><h1>Test ID verification</h1>"
        "<p>Development only: no real ID is checked.</p>"
        f"{body}</body></html>"
    )


@router.post("/{ref}", status_code=303, response_class=RedirectResponse)
async def mock_complete(ref: str, request: Request, kyc: Kyc) -> RedirectResponse:
    form = parse_qs((await request.body()).decode(errors="replace"))
    sig, outcome = form.get("sig", [""])[0], form.get("outcome", [""])[0]
    provider = _provider(request, ref, sig)
    if outcome not in OUTCOMES:
        raise BadRequest("Pick an outcome", error_type="invalid_outcome")
    attempt, user = await kyc.attempt_for_page(provider.name, ref)
    payload: dict[str, object] = {"ref": ref, "status": "failed"}
    if outcome != "failed":
        payload = {
            "ref": ref,
            "status": "success",
            "id_type": "aadhaar",
            "last4": f"{attempt.id.int % 10_000:04d}",
            "name": user.name if outcome == "verified" else MISMATCH_NAME,
            "dob": user.dob.isoformat() if user.dob else None,
        }
    raw = json.dumps(payload).encode()
    settings = request.app.state.settings
    response = await request.app.state.kyc_http.post(
        settings.KYC_MOCK_WEBHOOK_URL,
        content=raw,
        headers={"content-type": "application/json", provider.SIGNATURE_HEADER: provider.sign(raw)},
    )
    if response.status_code != 200:
        raise BadGateway("The webhook rejected the result", error_type="webhook_failed")
    return RedirectResponse(f"{settings.KYC_RETURN_URL}?attempt_id={attempt.id}", status_code=303)
