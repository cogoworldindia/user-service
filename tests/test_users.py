import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.inbox import ProcessedEvent
from cogo_common.events.outbox import OutboxMessage
from cogo_common.events.topology import EVENTS_EXCHANGE, USER_SERVICE, RoutingKeys
from fastapi import FastAPI
from sqlalchemy import select, update

from app.models import AccountStatus, User

Auth = Callable[[UUID], dict[str, str]]


async def _events(app: FastAPI, user_id: UUID, routing_key: str) -> list[dict]:
    async with app.state.db.sessionmaker() as s:
        rows = (
            await s.scalars(select(OutboxMessage).where(OutboxMessage.routing_key == routing_key))
        ).all()
    return [r.payload["data"] for r in rows if r.payload["data"].get("id") == str(user_id)]


async def _wait_for(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- app compatibility
async def test_legacy_endpoints_still_work(client: httpx.AsyncClient, as_user: Auth) -> None:
    h = as_user(uuid4())
    r = await client.get("/v1/users/me", headers=h)
    assert r.status_code == 200 and r.json()["data"]["profile"] is None
    r = await client.patch(
        "/v1/users/me/profile",
        headers=h,
        json={"full_name": "Asha Rao", "date_of_birth": "1996-04-02", "gender": "female"},
    )
    assert r.status_code == 200, r.text
    profile = r.json()["data"]["profile"]
    assert (profile["full_name"], profile["date_of_birth"], profile["gender"]) == (
        "Asha Rao",
        "1996-04-02",
        "female",
    )
    assert r.json()["data"]["status"] == "active"


# ---------------------------------------------------------------- /v1/me
async def test_me_created_and_user_created_event(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    r = await client.get("/v1/me", headers=as_user(uid))
    me = r.json()["data"]
    assert r.status_code == 200 and me["id"] == str(uid)
    assert me["stats"] == {"journeys": 0, "followers": 0, "following": 0}
    assert me["kyc_prompt"] == {"show": True, "variant": "none"} and me["guide_seen"] is False
    created = await _events(app, uid, "user.created")
    assert len(created) == 1 and created[0]["version"] == 1


async def test_patch_me_bumps_version_emits_snapshot_and_assigns_username(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    h = as_user(uid)
    tags = (await client.get("/v1/personality-tags", headers=h)).json()["data"]
    assert len(tags) == 16 and tags[0]["slug"] == "adventure"
    r = await client.patch(
        "/v1/me",
        headers=h,
        json={
            "name": "Shivam Singh",
            "dob": "1998-01-20",
            "gender": "male",
            "bio": "Hills > beaches",
            "profession": "Engineer",
            "city_place_id": "pl_noida",
            "personality_tag_ids": [t["id"] for t in tags[:3]],
        },
    )
    assert r.status_code == 200, r.text
    me = r.json()["data"]
    assert me["username"].startswith("shivamsingh") and me["age"] >= 28
    assert me["city"] == {"name": "Noida", "place_id": "pl_noida"}
    assert [t["slug"] for t in me["personality_tags"]] == ["adventure", "party", "photography"]
    updated = await _events(app, uid, "user.updated")
    snap = updated[-1]
    assert snap["version"] == 2 and snap["city"]["lat"] == 28.5355 and snap["gender"] == "male"
    assert "email" not in snap and "bio" not in snap  # events carry only what consumers need


async def test_patch_me_validation_and_conflicts(client: httpx.AsyncClient, as_user: Auth) -> None:
    a, b = as_user(uuid4()), as_user(uuid4())
    await client.patch("/v1/me", headers=a, json={"username": "unique.name1"})
    r = await client.patch(
        "/v1/me", headers=b, json={"username": "Unique.Name1"}
    )  # citext: case-insensitive
    assert r.status_code == 409 and r.json()["error"]["type"] == "username_taken"
    for body, status, etype in (
        ({"account_status": "banned"}, 422, "validation_error"),
        ({"username": "x"}, 422, "validation_error"),
        ({"personality_tag_ids": [1, 2]}, 422, "validation_error"),
        ({"personality_tag_ids": [1, 2, 999]}, 400, "invalid_tags"),
        ({"city_place_id": "pl_unknown"}, 400, "invalid_place"),
        ({"avatar_url": "https://evil.example.com/x.jpg"}, 400, "invalid_media"),
        ({}, 400, "empty_update"),
    ):
        r = await client.patch("/v1/me", headers=b, json=body)
        assert (r.status_code, r.json()["error"]["type"]) == (status, etype), (body, r.text)


async def test_guide_seen(client: httpx.AsyncClient, as_user: Auth) -> None:
    h = as_user(uuid4())
    assert (await client.post("/v1/me/guide/seen", headers=h)).status_code == 200
    assert (await client.get("/v1/me", headers=h)).json()["data"]["guide_seen"] is True


# ---------------------------------------------------------------- public profile
async def test_public_profile_hides_private_fields_and_degrades(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    owner, viewer = uuid4(), uuid4()
    await client.patch(
        "/v1/me",
        headers=as_user(owner),
        json={"name": "Owner", "dob": "1990-01-01", "gender": "male", "city_place_id": "pl_noida"},
    )
    app.state.social.result = {owner: True}
    app.state.chat.result = {owner: "connected"}
    r = await client.get(f"/v1/users/{owner}", headers=as_user(viewer))
    data = r.json()["data"]
    assert (
        r.status_code == 200
        and data["is_following"] is True
        and data["connection_status"] == "connected"
    )
    assert (
        "dob" not in data
        and "gender" not in data
        and data["city"] == {"name": "Noida", "place_id": None}
    )
    app.state.social.result = None  # social- and chat-service down -> null, not an error
    app.state.chat.result = None
    degraded = (await client.get(f"/v1/users/{owner}", headers=as_user(viewer))).json()["data"]
    assert degraded["is_following"] is None and degraded["connection_status"] is None
    app.state.chat.result = {}
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(
            update(User).where(User.id == owner).values(account_status=AccountStatus.SUSPENDED)
        )
    assert (await client.get(f"/v1/users/{owner}", headers=as_user(viewer))).status_code == 404


# ---------------------------------------------------------------- media
async def test_media_upload_confirm_and_use_as_avatar_and_cover(
    client: httpx.AsyncClient, as_user: Auth
) -> None:
    uid, other = uuid4(), uuid4()
    h = as_user(uid)
    r = await client.post(
        "/v1/media/upload-url", headers=h, json={"kind": "avatar", "content_type": "image/jpeg"}
    )
    up = r.json()["data"]
    assert (
        r.status_code == 200
        and up["file_url"].endswith(".jpg")
        and f"users/{uid}/avatar/" in up["file_url"]
    )
    assert (await client.post(f"/v1/media/{up['media_id']}/confirm", headers=h)).json()["error"][
        "type"
    ] == "upload_missing"
    async with httpx.AsyncClient() as raw:
        assert (
            await raw.put(
                up["upload_url"],
                content=b"\xff\xd8" + b"0" * 500,
                headers={"Content-Type": "image/jpeg"},
            )
        ).status_code == 200
    r = await client.post(f"/v1/media/{up['media_id']}/confirm", headers=h)
    assert r.json()["data"]["status"] == "ready"
    assert (
        await client.post(f"/v1/media/{up['media_id']}/confirm", headers=as_user(other))
    ).status_code == 404
    r = await client.patch("/v1/me", headers=h, json={"avatar_url": up["file_url"]})
    assert r.status_code == 200 and r.json()["data"]["avatar_url"] == up["file_url"]
    r = await client.patch(
        "/v1/me", headers=as_user(other), json={"avatar_url": up["file_url"]}
    )  # not yours
    assert r.json()["error"]["type"] == "invalid_media"

    cover = (
        await client.post(
            "/v1/media/upload-url", headers=h, json={"kind": "cover", "content_type": "image/png"}
        )
    ).json()["data"]
    async with httpx.AsyncClient() as raw:
        await raw.put(
            cover["upload_url"],
            content=b"\x89PNG" + b"0" * 100,
            headers={"Content-Type": "image/png"},
        )
    await client.post(f"/v1/media/{cover['media_id']}/confirm", headers=h)
    r = await client.put(
        "/v1/me/cover-photos", headers=h, json=[{"position": 0, "url": cover["file_url"]}]
    )
    assert r.status_code == 200 and r.json()["data"] == [{"position": 0, "url": cover["file_url"]}]


async def test_oversized_upload_rejected(client: httpx.AsyncClient, as_user: Auth) -> None:
    h = as_user(uuid4())
    up = (
        await client.post(
            "/v1/media/upload-url", headers=h, json={"kind": "avatar", "content_type": "image/jpeg"}
        )
    ).json()["data"]
    async with httpx.AsyncClient() as raw:
        await raw.put(
            up["upload_url"], content=b"0" * 20_000, headers={"Content-Type": "image/jpeg"}
        )
    r = await client.post(f"/v1/media/{up['media_id']}/confirm", headers=h)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_upload"


# ---------------------------------------------------------------- events in
async def test_registered_and_follow_events_are_idempotent(app: FastAPI) -> None:
    a, b = uuid4(), uuid4()
    broker = app.state.broker
    reg = EventEnvelope(
        type=RoutingKeys.USER_REGISTERED,
        producer="auth-service",
        data={"user_id": str(a), "provider": "email", "registered_at": "2026-11-19T00:00:00Z"},
    )
    follow = EventEnvelope(
        type=RoutingKeys.SOCIAL_FOLLOWED,
        producer="social-service",
        data={
            "follower_id": str(a),
            "followee_id": str(b),
            "follower": {"id": str(a), "name": "A"},
        },
    )
    for env in (reg, follow, follow):  # duplicate follow delivery
        await broker.publish(
            env.model_dump(mode="json"),
            exchange=EVENTS_EXCHANGE,
            routing_key=env.type,
            message_id=str(env.id),
        )

    async def done() -> bool:
        async with app.state.db.sessionmaker() as s:
            return await s.get(ProcessedEvent, follow.id) is not None

    await _wait_for(done)
    await asyncio.sleep(0.3)
    async with app.state.db.sessionmaker() as s:
        ua, ub = await s.get(User, a), await s.get(User, b)
    assert ua is not None and ub is not None
    assert (ua.following_count, ub.followers_count) == (1, 1)


async def test_poison_event_dead_lettered(app: FastAPI) -> None:
    broker = app.state.broker
    bad = EventEnvelope(
        type=RoutingKeys.SOCIAL_FOLLOWED, producer="x", data={"follower_id": "nope"}
    )
    await broker.publish(
        bad.model_dump(mode="json"),
        exchange=EVENTS_EXCHANGE,
        routing_key=bad.type,
        message_id=str(bad.id),
    )
    dlq = await broker.declare_queue(USER_SERVICE.dead_letter_queue())

    async def dead() -> bool:
        m = await dlq.get(fail=False, no_ack=True)
        return m is not None and m.message_id == str(bad.id)

    await _wait_for(dead)


async def test_requires_authentication(client: httpx.AsyncClient) -> None:
    for path in ("/v1/me", "/v1/personality-tags", f"/v1/users/{uuid4()}"):
        assert (await client.get(path)).status_code == 401


async def test_journey_events_maintain_journeys_count(app: FastAPI) -> None:
    owner = uuid4()
    place = {"place_id": "p", "name": "X", "lat": 1.0, "lng": 2.0}

    def journey_event(kind: str, jid: UUID) -> EventEnvelope:
        return EventEnvelope(
            type=kind,
            producer="journey-service",
            data={
                "id": str(jid),
                "owner_id": str(owner),
                "origin": place,
                "destination": place,
                "start_at": "2026-12-01T08:00:00Z",
                "end_at": "2026-12-02T08:00:00Z",
                "who_can_join": "anyone",
                "group_size": 4,
                "seats_taken": 1,
                "status": "upcoming",
                "version": 1,
            },
        )

    j1, j2 = uuid4(), uuid4()
    events = [
        journey_event("journey.created", j1),
        journey_event("journey.created", j2),
        journey_event("journey.deleted", j1),
    ]
    for env in events:
        await app.state.broker.publish(
            env.model_dump(mode="json"),
            exchange=EVENTS_EXCHANGE,
            routing_key=env.type,
            message_id=str(env.id),
        )

    async def done() -> bool:
        async with app.state.db.sessionmaker() as s:
            return await s.get(ProcessedEvent, events[-1].id) is not None

    await _wait_for(done)
    async with app.state.db.sessionmaker() as s:
        user = await s.get(User, owner)
    assert user is not None and user.journeys_count == 1


async def test_blocked_profiles_are_unavailable_both_ways(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    a, b = uuid4(), uuid4()
    for u in (a, b):
        await client.get("/v1/me", headers=as_user(u))

    async def block_event(kind: str, version: int) -> None:
        env = EventEnvelope(
            type=kind,
            producer="social-service",
            data={"blocker_id": str(a), "blocked_id": str(b), "version": version},
        )
        await app.state.broker.publish(
            env.model_dump(mode="json"),
            exchange=EVENTS_EXCHANGE,
            routing_key=env.type,
            message_id=str(env.id),
        )

        async def done() -> bool:
            async with app.state.db.sessionmaker() as s:
                return await s.get(ProcessedEvent, env.id) is not None

        await _wait_for(done)

    await block_event("social.user_blocked", 10)
    for viewer, target in ((a, b), (b, a)):
        r = await client.get(f"/v1/users/{target}", headers=as_user(viewer))
        assert r.status_code == 404 and r.json()["error"]["type"] == "profile_unavailable"
    await block_event("social.user_unblocked", 20)
    await block_event("social.user_blocked", 15)  # stale, older than the unblock
    assert (await client.get(f"/v1/users/{b}", headers=as_user(a))).status_code == 200
