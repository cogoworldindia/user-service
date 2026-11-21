"""eKYC (spec §5.9): prompt rules, official-email OTP, the government-ID flow through the
mock provider and its signed webhook, the derived level and its event, and the identity
lock. Spec §12 asks for kyc_level derivation tests: those live in cogo-common."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from cogo_common.appconfig import DEFAULTS, AppConfig, merge
from cogo_common.events.outbox import OutboxMessage
from cogo_common.events.topology import NOTIFICATION_EMAIL, RoutingKeys
from fastapi import FastAPI
from sqlalchemy import select, update

from app.kyc.mailer import KycMailer
from app.kyc.names import name_similarity
from app.kyc.service import email_domain_blocked, kyc_prompt
from app.models import KycAttempt, User
from tests.conftest import FakeMailer

Auth = Callable[[UUID], dict[str, str]]
MOCK_SIGNATURE = "x-mock-signature"


# ---------------------------------------------------------------- helpers
@pytest.fixture
def mailer(app: FastAPI) -> FakeMailer:
    fake: FakeMailer = app.state.kyc_mailer
    fake.fail = False
    return fake


@pytest.fixture
def kyc_config(app: FastAPI) -> Iterator[Callable[[dict[str, Any]], None]]:
    """Override `kyc` config for one test (what admin-service will write to Redis)."""
    original = app.state.config

    def set_kyc(override: dict[str, Any]) -> None:
        app.state.config = AppConfig(
            None, defaults={**DEFAULTS, "kyc": merge(DEFAULTS["kyc"], override)}
        )

    yield set_kyc
    app.state.config = original


async def kyc_events(app: FastAPI, user_id: UUID) -> list[dict[str, Any]]:
    async with app.state.db.sessionmaker() as s:
        rows = (
            await s.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.routing_key == RoutingKeys.USER_KYC_LEVEL_CHANGED)
                .order_by(OutboxMessage.created_at)
            )
        ).all()
    return [r.payload["data"] for r in rows if r.payload["data"]["id"] == str(user_id)]


async def profile(client: httpx.AsyncClient, h: dict[str, str], **fields: Any) -> None:
    body = {"name": "Asha Sharma", "dob": "1996-04-02", "gender": "female", **fields}
    r = await client.patch("/v1/me", headers=h, json=body)
    assert r.status_code == 200, r.text


async def kyc(client: httpx.AsyncClient, h: dict[str, str]) -> dict[str, Any]:
    r = await client.get("/v1/me/kyc", headers=h)
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


def err(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["type"]


async def verified_email(
    client: httpx.AsyncClient, mailer: FakeMailer, h: dict[str, str], email: str
) -> httpx.Response:
    r = await client.post("/v1/me/kyc/email/start", headers=h, json={"email": email})
    assert r.status_code == 202, r.text
    return await client.post(
        "/v1/me/kyc/email/verify", headers=h, json={"otp": mailer.code_for(email)}
    )


async def start_id(client: httpx.AsyncClient, h: dict[str, str]) -> dict[str, Any]:
    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={"consent": True})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


async def finish_id(client: httpx.AsyncClient, redirect_url: str, outcome: str) -> httpx.Response:
    """What the tester does on the mock provider's page."""
    url = urlsplit(redirect_url)
    page = await client.get(f"{url.path}?{url.query}")
    assert page.status_code == 200 and f'value="{outcome}"' in page.text
    sig = url.query.removeprefix("sig=")
    return await client.post(
        url.path,
        content=f"sig={sig}&outcome={outcome}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------- unit
@pytest.mark.parametrize(
    ("a", "b", "matches"),
    [
        ("Asha Sharma", "Sharma Asha", True),
        ("Asha Sharma", "ASHA KUMARI SHARMA", True),
        ("Smt. Asha Sharma", "asha sharma", True),
        ("Ravi Kumar", "Ravi Kumaar", True),
        ("Asha Rao", "Asha Sharma", False),
        ("Neha Gupta", "Kabir Singh", False),
        ("", "Asha Sharma", False),
        (None, "Asha Sharma", False),
    ],
)
def test_name_similarity(a: str | None, b: str, matches: bool) -> None:
    assert (name_similarity(a, b) >= 0.85) is matches


def test_email_domain_rule() -> None:
    blocked = ["gmail.com", "yahoo.com"]
    assert email_domain_blocked("a@gmail.com", blocked)
    assert email_domain_blocked("a@mail.yahoo.com", blocked)  # subdomains too
    assert not email_domain_blocked("a@iitd.ac.in", blocked)
    assert not email_domain_blocked("a@notgmail.com", blocked)


def test_prompt_variants() -> None:
    now = datetime.now(UTC)
    assert kyc_prompt(User(kyc_level="none")) == (True, "none")
    assert kyc_prompt(User(kyc_level="partial", email_verified_at=now)) == (True, "partial_need_id")
    # only reachable if product makes full require both checks (§14 default: ID alone = full)
    assert kyc_prompt(User(kyc_level="partial", gov_id_verified_at=now))[1] == "partial_need_email"
    assert kyc_prompt(User(kyc_level="full", gov_id_verified_at=now))[0] is False
    snoozed = User(kyc_level="none", kyc_prompt_snoozed_until=now + timedelta(days=1))
    assert kyc_prompt(snoozed)[0] is False
    assert kyc_prompt(User(kyc_level="none", kyc_review_status="pending_review"))[0] is False


# ---------------------------------------------------------------- prompt + snooze
async def test_prompt_shows_until_snoozed_and_returns_after_cooldown(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    me = (await client.get("/v1/me", headers=h)).json()["data"]
    assert me["kyc_prompt"] == {"show": True, "variant": "none"} and me["identity_locked"] is False
    status = await kyc(client, h)
    assert status["email"]["status"] == status["gov_id"]["status"] == "not_started"

    r = await client.post("/v1/me/kyc/prompt/snooze", headers=h)
    assert r.status_code == 200 and r.json()["data"]["kyc_prompt"]["show"] is False
    until = datetime.fromisoformat(r.json()["data"]["snoozed_until"])
    assert timedelta(days=2, hours=23) < until - datetime.now(UTC) <= timedelta(days=3)
    assert (await client.get("/v1/me", headers=h)).json()["data"]["kyc_prompt"]["show"] is False

    async with app.state.db.sessionmaker() as s, s.begin():  # the cool-down passes
        await s.execute(
            update(User)
            .where(User.id == uid)
            .values(kyc_prompt_snoozed_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await client.get("/v1/me", headers=h)).json()["data"]["kyc_prompt"]["show"] is True


async def test_snooze_cooldown_comes_from_config(
    client: httpx.AsyncClient, as_user: Auth, kyc_config: Callable[[dict[str, Any]], None]
) -> None:
    kyc_config({"prompt_cooldown_days": 7})
    r = await client.post("/v1/me/kyc/prompt/snooze", headers=as_user(uuid4()))
    until = datetime.fromisoformat(r.json()["data"]["snoozed_until"])
    assert until - datetime.now(UTC) > timedelta(days=6, hours=23)


# ---------------------------------------------------------------- official email
async def test_official_email_makes_the_user_partial(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI, mailer: FakeMailer
) -> None:
    uid = uuid4()
    h = as_user(uid)
    for personal in ("asha@gmail.com", "asha@mail.yahoo.com"):
        r = await client.post("/v1/me/kyc/email/start", headers=h, json={"email": personal})
        assert err(r) == (400, "email_domain_not_allowed")

    r = await client.post("/v1/me/kyc/email/start", headers=h, json={"email": "Asha@IITD.ac.in"})
    assert r.status_code == 202 and r.json()["data"]["email"] == "asha@iitd.ac.in"
    status = await kyc(client, h)
    assert status["email"]["status"] == "code_sent"
    assert status["email"]["pending_email"] == "asha@iitd.ac.in"

    code = mailer.code_for("asha@iitd.ac.in")
    wrong = "000000" if code != "000000" else "111111"
    r = await client.post("/v1/me/kyc/email/verify", headers=h, json={"otp": wrong})
    assert err(r) == (400, "otp_invalid") and r.json()["error"]["details"] == {"attempts_left": 4}
    r = await client.post("/v1/me/kyc/email/verify", headers=h, json={"otp": "12ab56"})
    assert r.status_code == 422

    r = await client.post("/v1/me/kyc/email/verify", headers=h, json={"otp": code})
    assert r.status_code == 200, r.text
    status = r.json()["data"]
    assert status["kyc_level"] == "partial"
    assert status["email"]["status"] == "verified" and status["email"]["email"] == "asha@iitd.ac.in"
    assert status["kyc_prompt"] == {"show": True, "variant": "partial_need_id"}

    [event] = await kyc_events(app, uid)
    assert event["kyc_level"] == "partial" and event["previous_kyc_level"] == "none"
    assert event["version"] >= 2 and "iitd" not in json.dumps(event)  # only the level travels

    r = await client.post("/v1/me/kyc/email/start", headers=h, json={"email": "asha@iitd.ac.in"})
    assert err(r) == (409, "already_verified")


async def test_otp_lockout_expiry_supersede_and_hourly_limit(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI, mailer: FakeMailer
) -> None:
    uid = uuid4()
    h = as_user(uid)
    email = "ravi@corp.example.in"

    async def send() -> httpx.Response:
        return await client.post("/v1/me/kyc/email/start", headers=h, json={"email": email})

    async def verify(otp: str) -> httpx.Response:
        return await client.post("/v1/me/kyc/email/verify", headers=h, json={"otp": otp})

    # 1st code: five wrong guesses burn it, and the right code no longer works
    assert (await send()).status_code == 202
    code = mailer.code_for(email)
    wrong = "000000" if code != "000000" else "111111"
    results = [err(await verify(wrong)) for _ in range(5)]
    assert results == [(400, "otp_invalid")] * 4 + [(429, "otp_locked")]
    assert err(await verify(code)) == (400, "otp_invalid")

    # 2nd code: sending a 3rd supersedes it
    assert (await send()).status_code == 202
    second = mailer.code_for(email)
    assert (await send()).status_code == 202
    third = mailer.code_for(email)
    if second != third:
        assert err(await verify(second)) == (400, "otp_invalid")

    # 3rd code expires
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(
            update(KycAttempt)
            .where(KycAttempt.user_id == uid, KycAttempt.status == "otp_sent")
            .values(otp_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert err(await verify(third)) == (400, "otp_expired")

    # spec §5.9: at most 3 sends an hour
    r = await send()
    assert err(r) == (429, "otp_send_limit") and int(r.headers["retry-after"]) > 0
    assert (await kyc(client, h))["kyc_level"] == "none"


async def test_email_verified_on_one_account_only_and_failed_sends_dont_count(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI, mailer: FakeMailer
) -> None:
    first, second = as_user(uuid4()), as_user(uuid4())
    email = f"shared-{uuid4().hex[:6]}@corp.example.in"
    assert (await verified_email(client, mailer, first, email)).status_code == 200
    r = await verified_email(client, mailer, second, email)
    assert err(r) == (409, "email_in_use")

    uid = uuid4()
    mailer.fail = True
    r = await client.post(
        "/v1/me/kyc/email/start", headers=as_user(uid), json={"email": "x@corp.example.in"}
    )
    assert err(r) == (503, "unavailable")
    async with app.state.db.sessionmaker() as s:
        assert (await s.scalars(select(KycAttempt).where(KycAttempt.user_id == uid))).all() == []


async def test_domain_rule_is_config(
    client: httpx.AsyncClient,
    as_user: Auth,
    mailer: FakeMailer,
    kyc_config: Callable[[dict[str, Any]], None],
) -> None:
    kyc_config({"require_official_domain": False})
    r = await verified_email(client, mailer, as_user(uuid4()), f"me{uuid4().hex[:6]}@gmail.com")
    assert r.status_code == 200 and r.json()["data"]["kyc_level"] == "partial"


async def test_real_mailer_queues_the_code_for_notification_service(app: FastAPI) -> None:
    queue = await app.state.broker.declare_queue(NOTIFICATION_EMAIL.queue())
    await queue.purge()
    await KycMailer(app.state.broker, producer="user-service").send_code(
        "asha@iitd.ac.in", "135790", ttl_seconds=600
    )
    message = await queue.get(timeout=5)
    await message.ack()
    data = json.loads(message.body)["data"]
    assert data["template"] == "kyc_email_otp" and data["to"] == "asha@iitd.ac.in"
    assert data["params"] == {"code": "135790", "ttl_minutes": 10} and data["expires_at"]


# ---------------------------------------------------------------- government ID (mock provider)
async def test_mock_id_flow_verifies_and_locks_the_identity(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI, mailer: FakeMailer
) -> None:
    uid = uuid4()
    h = as_user(uid)
    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={"consent": True})
    assert err(r) == (400, "profile_incomplete")
    await profile(client, h)
    assert (await verified_email(client, mailer, h, "asha.s@iitd.ac.in")).status_code == 200
    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={})
    assert r.status_code == 422  # the consent screen must be accepted

    started = await start_id(client, h)
    assert started["redirect_url"].startswith("http://user/v1/kyc/mock/mock_")
    assert (await kyc(client, h))["gov_id"] == {
        "status": "pending",
        "attempt_id": started["attempt_id"],
        "id_type": None,
        "last4": None,
        "verified_at": None,
    }
    path = urlsplit(started["redirect_url"]).path
    assert (await client.get(f"{path}?sig=forged")).status_code == 404

    r = await finish_id(client, started["redirect_url"], "verified")
    assert r.status_code == 303
    assert r.headers["location"] == f"cogo://kyc/return?attempt_id={started['attempt_id']}"

    status = await kyc(client, h)
    assert status["kyc_level"] == "full" and status["identity_locked"] is True
    assert status["gov_id"]["status"] == "verified" and status["gov_id"]["id_type"] == "aadhaar"
    assert len(status["gov_id"]["last4"]) == 4 and status["kyc_prompt"]["show"] is False
    levels = [(e["previous_kyc_level"], e["kyc_level"]) for e in await kyc_events(app, uid)]
    assert levels == [("none", "partial"), ("partial", "full")]

    # spec §5.9: a verified name and DOB are read-only (other fields aren't)
    r = await client.patch("/v1/me", headers=h, json={"name": "Someone Else"})
    assert err(r) == (403, "identity_locked")
    r = await client.patch("/v1/me", headers=h, json={"dob": "1990-01-01"})
    assert err(r) == (403, "identity_locked")
    r = await client.patch("/v1/me", headers=h, json={"name": "Asha Sharma", "bio": "Hills"})
    assert r.status_code == 200 and r.json()["data"]["identity_locked"] is True

    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={"consent": True})
    assert err(r) == (409, "already_verified")
    page = urlsplit(started["redirect_url"])
    assert "already finished" in (await client.get(f"{page.path}?{page.query}")).text


async def test_mismatch_goes_to_review_and_failure_can_be_retried(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    await profile(client, h, name="Neha Gupta")
    started = await start_id(client, h)
    assert (await finish_id(client, started["redirect_url"], "mismatch")).status_code == 303
    status = await kyc(client, h)
    assert status["gov_id"]["status"] == "review" and status["kyc_level"] == "none"
    assert status["kyc_prompt"]["show"] is False and status["identity_locked"] is True
    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={"consent": True})
    assert err(r) == (409, "kyc_review_pending")
    r = await client.patch("/v1/me", headers=h, json={"name": "Ramesh Iyer"})
    assert err(r) == (403, "identity_locked")  # can't edit the profile to fit the ID
    async with app.state.db.sessionmaker() as s:
        attempt = await s.get(KycAttempt, UUID(started["attempt_id"]))
    assert attempt is not None and attempt.status == "review"
    # spec §5.9: store only the match booleans, id type and last 4 (never the ID's name/DOB)
    assert attempt.result is not None and set(attempt.result) == {
        "name_match",
        "dob_match",
        "id_type",
        "last4",
    }
    assert attempt.result["name_match"] is False and attempt.result["dob_match"] is True
    assert await kyc_events(app, uid) == []

    other = as_user(uuid4())
    await profile(client, other, name="Kabir Singh", gender="male")
    failed = await start_id(client, other)
    assert (await finish_id(client, failed["redirect_url"], "failed")).status_code == 303
    assert (await kyc(client, other))["gov_id"]["status"] == "failed"
    retry = await start_id(client, other)
    assert (await finish_id(client, retry["redirect_url"], "verified")).status_code == 303
    assert (await kyc(client, other))["kyc_level"] == "full"


async def test_abandoned_session_expires_and_daily_limit(
    client: httpx.AsyncClient,
    as_user: Auth,
    app: FastAPI,
    kyc_config: Callable[[dict[str, Any]], None],
) -> None:
    uid = uuid4()
    h = as_user(uid)
    await profile(client, h)
    started = await start_id(client, h)
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(
            update(KycAttempt)
            .where(KycAttempt.id == UUID(started["attempt_id"]))
            .values(created_at=datetime.now(UTC) - timedelta(minutes=31))
        )
    assert (await kyc(client, h))["gov_id"]["status"] == "expired"

    kyc_config({"gov_id_attempts_per_day": 2})
    await start_id(client, h)  # the second one today
    r = await client.post("/v1/me/kyc/gov-id/start", headers=h, json={"consent": True})
    assert err(r) == (429, "kyc_attempt_limit")


async def test_webhook_checks_the_signature_and_is_idempotent(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    provider = app.state.kyc_provider

    async def deliver(payload: Any, *, signature: str | None = None) -> httpx.Response:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return await client.post(
            "/v1/webhooks/kyc/mock",
            content=raw,
            headers={MOCK_SIGNATURE: signature or provider.sign(raw)},
        )

    uid = uuid4()
    h = as_user(uid)
    await profile(client, h, name="Ravi Kumar", gender="male", dob="1994-08-15")
    started = await start_id(client, h)
    ref = f"mock_{UUID(started['attempt_id']).hex}"
    result = {
        "ref": ref,
        "status": "success",
        "id_type": "aadhaar",
        "last4": "5647",
        "name": "RAVI KUMAAR",  # spelling differs; still a match
        "dob": "1994-08-15",
    }
    assert err(await deliver(result, signature="0" * 64)) == (401, "invalid_signature")
    latin = await client.post(  # a non-ASCII header value, as raw bytes on the wire
        "/v1/webhooks/kyc/mock",
        content=json.dumps(result).encode(),
        headers={MOCK_SIGNATURE: ("é" * 64).encode("latin-1")},
    )
    assert err(latin) == (401, "invalid_signature")
    # the mock page with a non-ASCII signature or ref: 404, not a 500 (found by fuzzing)
    for path in (f"/v1/kyc/mock/{ref}?sig=%C3%A9", "/v1/kyc/mock/%C2%B3%C3%85?sig=x"):
        assert (await client.get(path)).status_code == 404
    assert (await client.post("/v1/webhooks/kyc/cashfree", content=b"{}")).status_code == 404
    assert err(await deliver(b"not json")) == (400, "invalid_payload")
    assert err(await deliver({"ref": ref})) == (400, "invalid_payload")
    assert (await deliver({**result, "ref": "mock_unknown"})).status_code == 200  # acked, ignored

    for _ in range(2):  # providers deliver at least once
        r = await deliver(result)
        assert r.status_code == 200 and r.json()["data"] == {"received": True}
    status = await kyc(client, h)
    assert status["kyc_level"] == "full" and status["gov_id"]["last4"] == "5647"
    assert len(await kyc_events(app, uid)) == 1

    async with app.state.db.sessionmaker() as s:  # only the outcome is kept on the user
        user = await s.get(User, uid)
    assert user is not None and user.dob == date(1994, 8, 15) and user.gov_id_type == "aadhaar"


async def test_kyc_endpoints_need_a_user(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/v1/me/kyc"),
        ("POST", "/v1/me/kyc/prompt/snooze"),
        ("POST", "/v1/me/kyc/email/start"),
        ("POST", "/v1/me/kyc/email/verify"),
        ("POST", "/v1/me/kyc/gov-id/start"),
    ):
        assert (await client.request(method, path, json={})).status_code == 401
