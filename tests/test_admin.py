"""M7 in user-service: admin actions over /internal/admin/* (status, KYC review, photos),
image moderation, and account deletion with its tracker (spec §5.10, §6.4, §7)."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from cogo_common.auth.service_tokens import ServiceTokenSigner
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.inbox import ProcessedEvent
from cogo_common.events.outbox import OutboxMessage
from cogo_common.events.topology import EVENTS_EXCHANGE, RoutingKeys
from cogo_common.internal_http import InternalClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import select, update

from app.deletion import DeletionTracker
from app.models import KycAttempt, MediaUpload, User, UserCoverPhoto
from app.moderation import MOCK_FLAG_MARKER, is_flagged
from tests.conftest import mint_reauth, service_headers
from tests.test_kyc import finish_id, profile, start_id

Auth = Callable[[UUID], dict[str, str]]
ADMIN = uuid4()


def admin() -> dict[str, str]:
    return service_headers("admin-service", ADMIN)


async def events(app: FastAPI, key: str, user_id: UUID) -> list[dict[str, Any]]:
    async with app.state.db.sessionmaker() as s:
        rows = (
            await s.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.routing_key == key)
                .order_by(OutboxMessage.created_at)
            )
        ).all()
    found = []
    for row in rows:
        data = row.payload["data"]
        if str(user_id) in (data.get("id"), data.get("owner_id")):
            found.append(data)
    return found


async def eventually(probe: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await probe()
        if value or asyncio.get_running_loop().time() > deadline:
            return value
        await asyncio.sleep(0.1)


async def publish(app: FastAPI, event_type: str, data: dict[str, Any]) -> None:
    env = EventEnvelope(type=event_type, producer="tests", data=data)
    await app.state.broker.publish(
        env.model_dump(mode="json"),
        exchange=EVENTS_EXCHANGE,
        routing_key=env.type,
        message_id=str(env.id),
    )

    async def processed() -> bool:
        async with app.state.db.sessionmaker() as s:
            return await s.get(ProcessedEvent, env.id) is not None

    assert await eventually(processed), f"{event_type} not processed"


async def upload(
    client: httpx.AsyncClient, h: dict[str, str], kind: str, content: bytes
) -> dict[str, Any]:
    up: dict[str, Any] = (
        await client.post(
            "/v1/media/upload-url", headers=h, json={"kind": kind, "content_type": "image/png"}
        )
    ).json()["data"]
    async with httpx.AsyncClient() as raw:
        r = await raw.put(up["upload_url"], content=content, headers={"Content-Type": "image/png"})
        assert r.status_code == 200
    r = await client.post(f"/v1/media/{up['media_id']}/confirm", headers=h)
    assert r.json()["data"]["status"] == "ready"
    return up


# ---------------------------------------------------------------- internal auth
async def test_only_admin_service_may_call(client: httpx.AsyncClient, as_user: Auth) -> None:
    uid = uuid4()
    await client.get("/v1/me", headers=as_user(uid))
    assert (await client.get(f"/internal/admin/users/{uid}")).status_code == 401
    r = await client.get(f"/internal/admin/users/{uid}", headers=service_headers("journey-service"))
    assert r.status_code == 403
    assert (
        await client.get(f"/internal/admin/users/{uid}", headers=as_user(uid))
    ).status_code == 401
    assert (await client.get(f"/internal/admin/users/{uid}", headers=admin())).status_code == 200
    # writes need the acting admin's id for the logs
    no_admin = service_headers("admin-service")
    r = await client.post(
        f"/internal/admin/users/{uid}/status",
        headers=no_admin,
        json={"action": "ban", "reason": "x"},
    )
    assert r.status_code == 422


async def test_search_and_detail(client: httpx.AsyncClient, as_user: Auth) -> None:
    uid = uuid4()
    tag = uuid4().hex[:6]
    await profile(client, as_user(uid), name=f"Zed {tag}", username=f"zed_{tag}")
    by_prefix = (
        await client.get(f"/internal/admin/users?q=zed_{tag[:3]}", headers=admin())
    ).json()["data"]
    assert str(uid) in [u["id"] for u in by_prefix]
    by_id = (await client.get(f"/internal/admin/users?q={uid}", headers=admin())).json()["data"]
    assert [u["id"] for u in by_id] == [str(uid)]
    assert (await client.get("/internal/admin/users?q=z", headers=admin())).json()["error"][
        "type"
    ] == "query_too_short"
    detail = (await client.get(f"/internal/admin/users/{uid}", headers=admin())).json()["data"]
    assert detail["dob"] == "1996-04-02" and detail["account_status"] == "active"
    assert detail["stats"] == {"followers": 0, "following": 0, "journeys": 0}
    assert (
        await client.get(f"/internal/admin/users/{uuid4()}", headers=admin())
    ).status_code == 404


# ---------------------------------------------------------------- status (spec §5.10)
async def test_suspend_ban_reinstate_and_expiry(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    await profile(client, h)

    async def status(body: dict[str, Any]) -> httpx.Response:
        return await client.post(f"/internal/admin/users/{uid}/status", headers=admin(), json=body)

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert (await status({"action": "suspend", "until": past, "reason": "x"})).json()["error"][
        "type"
    ] == "invalid_until"
    assert (await status({"action": "reinstate", "reason": "x"})).json()["error"][
        "type"
    ] == "invalid_status"
    until = datetime.now(UTC) + timedelta(days=7)
    r = await status({"action": "suspend", "until": until.isoformat(), "reason": "spam"})
    assert r.status_code == 200 and r.json()["data"]["account_status"] == "suspended"
    [event] = await events(app, RoutingKeys.USER_STATUS_CHANGED, uid)
    assert event["account_status"] == "suspended" and event["previous_status"] == "active"
    assert (
        datetime.fromisoformat(event["suspended_until"]) == until and event["name"] == "Asha Sharma"
    )
    # a suspended user can't change anything, and their profile is gone for others
    assert (await client.patch("/v1/me", headers=h, json={"bio": "x"})).json()["error"][
        "type"
    ] == "account_inactive"
    assert (await client.get(f"/v1/users/{uid}", headers=as_user(uuid4()))).status_code == 404

    async with app.state.db.sessionmaker() as s, s.begin():  # the suspension runs out
        await s.execute(
            update(User).where(User.id == uid).values(suspended_until=datetime.now(UTC))
        )
    from app.admin import AdminUsers

    async with app.state.db.sessionmaker() as s:
        assert await AdminUsers(s).expire_suspensions() >= 1
    statuses = [
        (e["previous_status"], e["account_status"])
        for e in await events(app, RoutingKeys.USER_STATUS_CHANGED, uid)
    ]
    assert statuses == [("active", "suspended"), ("suspended", "active")]
    assert (await client.get(f"/v1/users/{uid}", headers=as_user(uuid4()))).status_code == 200

    r = await status({"action": "ban", "reason": "scam"})
    assert (
        r.json()["data"]["account_status"] == "banned"
        and r.json()["data"]["suspended_until"] is None
    )
    assert (await status({"action": "reinstate", "reason": "appeal"})).json()["data"][
        "account_status"
    ] == "active"


# ---------------------------------------------------------------- KYC review (spec §5.9)
async def test_kyc_review_approve_and_reject(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    approved, rejected = uuid4(), uuid4()
    for uid, name in ((approved, "Meera Pillai"), (rejected, "Arjun Nair")):
        await profile(client, as_user(uid), name=name)
        assert (
            await finish_id(
                client, (await start_id(client, as_user(uid)))["redirect_url"], "mismatch"
            )
        ).status_code == 303
    # admin-service asks owners for one more than its page (50) to see if there is a next page
    queue = (await client.get("/internal/admin/kyc-reviews?limit=51", headers=admin())).json()[
        "data"
    ]
    mine = {r["user_id"]: r for r in queue if r["user_id"] in (str(approved), str(rejected))}
    assert set(mine) == {str(approved), str(rejected)}
    assert (
        mine[str(approved)]["result"]["name_match"] is False
        and mine[str(approved)]["result"]["dob_match"] is True
    )

    r = await client.post(
        f"/internal/admin/kyc-reviews/{approved}/approve", headers=admin(), json={"note": "checked"}
    )
    assert r.json()["data"] == {
        "user_id": str(approved),
        "kyc_level": "full",
        "kyc_review_status": None,
    }
    [event] = await events(app, RoutingKeys.USER_KYC_LEVEL_CHANGED, approved)
    assert (event["previous_kyc_level"], event["kyc_level"]) == ("none", "full")
    assert (await client.get("/v1/me/kyc", headers=as_user(approved))).json()["data"]["gov_id"][
        "status"
    ] == "verified"
    again = await client.post(
        f"/internal/admin/kyc-reviews/{approved}/approve", headers=admin(), json={"note": "x"}
    )
    assert again.json()["error"]["type"] == "not_in_review"

    r = await client.post(
        f"/internal/admin/kyc-reviews/{rejected}/reject", headers=admin(), json={"note": "not them"}
    )
    assert r.json()["data"]["kyc_review_status"] == "rejected"
    status = (await client.get("/v1/me/kyc", headers=as_user(rejected))).json()["data"]
    assert status["gov_id"]["status"] == "rejected" and status["identity_locked"] is False
    # they can fix the profile and try again
    assert (
        await client.patch("/v1/me", headers=as_user(rejected), json={"name": "Ramesh Iyer"})
    ).status_code == 200
    await start_id(client, as_user(rejected))
    assert (await client.get("/v1/me/kyc", headers=as_user(rejected))).json()["data"]["gov_id"][
        "status"
    ] == "pending"


# ---------------------------------------------------------------- image moderation (spec §5.10)
def test_flag_rule() -> None:
    assert is_flagged({"adult": "LIKELY"}) and is_flagged({"violence": "VERY_LIKELY"})
    assert not is_flagged({"adult": "POSSIBLE", "racy": "VERY_LIKELY"})  # racy isn't a flag
    assert not is_flagged({}) and not is_flagged({"adult": "weird"})


async def test_flagged_photos_are_taken_down(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    clean = await upload(client, h, "avatar", b"\x89PNG" + b"0" * 200)
    bad_avatar = await upload(client, h, "avatar", b"\x89PNG" + MOCK_FLAG_MARKER + b"0" * 200)
    bad_cover = await upload(client, h, "cover", b"\x89PNG" + MOCK_FLAG_MARKER + b"1" * 200)
    await client.patch("/v1/me", headers=h, json={"avatar_url": bad_avatar["file_url"]})
    await client.put(
        "/v1/me/cover-photos", headers=h, json=[{"position": 0, "url": bad_cover["file_url"]}]
    )

    async def taken_down() -> bool:
        me = (await client.get("/v1/me", headers=h)).json()["data"]
        return me["avatar_url"] is None and me["cover_photos"] == []

    assert await eventually(taken_down)
    flagged = await events(app, RoutingKeys.MEDIA_FLAGGED, uid)
    assert {f["media_id"] for f in flagged} == {bad_avatar["media_id"], bad_cover["media_id"]}
    assert flagged[0]["labels"]["adult"] == "VERY_LIKELY"
    async with app.state.db.sessionmaker() as s:
        statuses = {
            str(m.id): m.status
            for m in (await s.scalars(select(MediaUpload).where(MediaUpload.user_id == uid))).all()
        }
    assert statuses == {
        clean["media_id"]: "ready",
        bad_avatar["media_id"]: "flagged",
        bad_cover["media_id"]: "flagged",
    }
    # a flagged upload can't be put back
    r = await client.patch("/v1/me", headers=h, json={"avatar_url": bad_avatar["file_url"]})
    assert r.json()["error"]["type"] == "invalid_media"
    assert (
        await client.patch("/v1/me", headers=h, json={"avatar_url": clean["file_url"]})
    ).status_code == 200


async def test_admin_and_report_takedowns(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    photo = await upload(client, h, "avatar", b"\x89PNG" + b"2" * 200)
    await client.patch("/v1/me", headers=h, json={"avatar_url": photo["file_url"]})
    preview = (
        await client.get(f"/internal/admin/media/{photo['media_id']}", headers=admin())
    ).json()["data"]
    assert preview["url"] == photo["file_url"] and preview["owner"]["id"] == str(uid)
    assert (
        await client.post(f"/internal/admin/media/{photo['media_id']}/remove", headers=admin())
    ).status_code == 200
    assert (await client.get("/v1/me", headers=h)).json()["data"]["avatar_url"] is None
    assert (
        await events(app, RoutingKeys.MEDIA_FLAGGED, uid) == []
    )  # already reported: no system report

    cover = await upload(client, h, "cover", b"\x89PNG" + b"3" * 200)
    await client.put(
        "/v1/me/cover-photos", headers=h, json=[{"position": 1, "url": cover["file_url"]}]
    )
    target = {
        "target_type": "media",
        "target_id": cover["media_id"],
        "target_owner_id": str(uid),
        "reason": "auto_hide",
    }
    await publish(app, RoutingKeys.SAFETY_CONTENT_HIDDEN, target)
    assert (await client.get("/v1/me", headers=h)).json()["data"]["cover_photos"] == []
    await publish(app, RoutingKeys.SAFETY_CONTENT_UNHIDDEN, {**target, "reason": "dismissed"})
    r = await client.put(
        "/v1/me/cover-photos", headers=h, json=[{"position": 1, "url": cover["file_url"]}]
    )
    assert r.status_code == 200  # usable again after an admin dismissed the reports


# ---------------------------------------------------------------- deletion (spec §7)
async def test_account_deletion_needs_a_fresh_code_and_anonymises(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    await profile(client, h, bio="Hills", username=f"del_{uid.hex[:8]}")
    photo = await upload(client, h, "cover", b"\x89PNG" + b"4" * 200)
    await client.put(
        "/v1/me/cover-photos", headers=h, json=[{"position": 0, "url": photo["file_url"]}]
    )
    await start_id(client, h)

    assert (await client.delete("/v1/me", headers=h)).json()["error"]["type"] == "reauth_required"
    wrong = [mint_reauth(uuid4()), mint_reauth(uid, purpose="something_else")]
    for token in wrong:
        r = await client.delete("/v1/me", headers={**h, "X-Reauth-Token": token})
        assert r.json()["error"]["type"] == "reauth_required"
    r = await client.delete(
        "/v1/me", headers={**h, "X-Reauth-Token": mint(uid)}
    )  # an access token isn't one
    assert r.status_code == 401

    r = await client.delete("/v1/me", headers={**h, "X-Reauth-Token": mint_reauth(uid)})
    assert r.status_code == 202 and r.json()["data"]["status"] == "deleting"
    deleted_at = r.json()["data"]["deleted_at"]
    again = await client.delete("/v1/me", headers={**h, "X-Reauth-Token": mint_reauth(uid)})
    assert (
        again.status_code == 202 and again.json()["data"]["deleted_at"] == deleted_at
    )  # idempotent
    assert (await client.get("/v1/me", headers=h)).status_code == 404
    assert (await client.get(f"/v1/users/{uid}", headers=as_user(uuid4()))).status_code == 404

    [event] = await events(app, RoutingKeys.USER_DELETED, uid)
    assert set(event) == {"id", "deleted_at"}  # nothing else travels
    async with app.state.db.sessionmaker() as s:
        user = await s.get(User, uid)
        assert user is not None
        assert (user.name, user.username, user.bio, user.dob, user.account_status.value) == (
            "Deleted user",
            None,
            None,
            None,
            "deleted",
        )
        for model in (UserCoverPhoto, KycAttempt, MediaUpload):
            assert (await s.scalars(select(model).where(model.user_id == uid))).all() == []
    async with httpx.AsyncClient() as raw:  # the photo is gone from storage
        assert (await raw.get(photo["file_url"])).status_code in (403, 404)


def mint(user_id: UUID) -> str:
    from tests.conftest import mint as access

    return access(user_id)


async def test_deletion_tracker(app: FastAPI) -> None:
    uid = uuid4()
    async with app.state.db.sessionmaker() as s, s.begin():
        s.add(
            User(
                id=uid,
                deleted_at=datetime.now(UTC) - timedelta(hours=30),
                account_status="deleted",
                name="Deleted user",
            )
        )
    done = {"auth": True, "journey": True, "social": False, "chat": True, "notification": True}
    seen: list[str] = []

    def client_for(name: str) -> InternalClient:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{name} {request.url.path}")
            return httpx.Response(200, json={"data": {"done": done[name]}})

        return InternalClient(
            f"http://{name}",
            audience=f"{name}-service",
            signer=ServiceTokenSigner("user-service", Ed25519PrivateKey.generate()),
            transport=httpx.MockTransport(handler),
        )

    tracker = DeletionTracker(app.state.db.sessionmaker, {n: client_for(n) for n in done})
    assert await tracker.pending(uid) == ["social"]
    await tracker.run_once()
    assert f"social /internal/admin/deletion-status/{uid}" in seen
    async with app.state.db.sessionmaker() as s:
        assert (await s.get(User, uid)).deletion_completed_at is None  # type: ignore[union-attr]
    done["social"] = True
    await tracker.run_once()
    async with app.state.db.sessionmaker() as s:
        assert (await s.get(User, uid)).deletion_completed_at is not None  # type: ignore[union-attr]
