import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.inbox import ProcessedEvent
from cogo_common.events.topology import EVENTS_EXCHANGE, RoutingKeys
from fastapi import FastAPI
from sqlalchemy import func, select, update

from app.models import User, UserStatus

Auth = Callable[[UUID], dict[str, str]]


async def test_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/users/me")).status_code == 401
    r = await client.get("/v1/users/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["type"] == "invalid_token"


async def test_me_is_created_on_first_request(client: httpx.AsyncClient, as_user: Auth) -> None:
    uid = uuid4()
    r = await client.get("/v1/users/me", headers=as_user(uid))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == str(uid) and data["status"] == "active" and data["profile"] is None


async def test_profile_patch_semantics(client: httpx.AsyncClient, as_user: Auth) -> None:
    uid = uuid4()
    h = as_user(uid)
    r = await client.patch(
        "/v1/users/me/profile",
        headers=h,
        json={
            "full_name": "  Asha Rao ",
            "date_of_birth": "1996-04-02",
            "gender": "female",
            "bio": "Trains > planes",
        },
    )
    assert r.status_code == 200, r.text
    profile = r.json()["data"]["profile"]
    assert profile["full_name"] == "Asha Rao" and profile["gender"] == "female"

    # omitted fields untouched, explicit null clears
    r = await client.patch("/v1/users/me/profile", headers=h, json={"bio": None})
    profile = r.json()["data"]["profile"]
    assert profile["bio"] is None and profile["full_name"] == "Asha Rao"


async def test_profile_validation(client: httpx.AsyncClient, as_user: Auth) -> None:
    h = as_user(uuid4())
    for body in (
        {"status": "deleted"},  # users cannot change their status
        {"user_id": str(uuid4())},  # ...or target someone else
        {"date_of_birth": "2999-01-01"},
        {"profile_photo_url": "http://insecure.example.com/a.png"},
        {"profile_photo_url": "javascript:alert(1)"},
        {"full_name": "x" * 101},
        {"gender": "robot"},
    ):
        r = await client.patch("/v1/users/me/profile", headers=h, json=body)
        assert r.status_code == 422, (body, r.text)
    assert (await client.patch("/v1/users/me/profile", headers=h, json={})).status_code == 400


async def test_other_users_only_get_public_projection(
    client: httpx.AsyncClient, as_user: Auth
) -> None:
    owner, viewer = uuid4(), uuid4()
    await client.patch(
        "/v1/users/me/profile",
        headers=as_user(owner),
        json={
            "full_name": "Owner",
            "date_of_birth": "1990-01-01",
            "gender": "male",
            "profile_photo_url": "https://cdn.example.com/p.jpg",
        },
    )
    r = await client.get(f"/v1/users/{owner}", headers=as_user(viewer))
    assert r.status_code == 200
    assert r.json()["data"] == {
        "id": str(owner),
        "full_name": "Owner",
        "bio": None,
        "profile_photo_url": "https://cdn.example.com/p.jpg",
    }
    # The old IDOR routes no longer exist.
    for method, path in (
        ("PUT", f"/v1/users/user/{owner}"),
        ("PUT", f"/v1/users/{owner}/profile"),
        ("PATCH", f"/v1/users/{owner}/profile"),
        ("POST", "/v1/users/user"),
    ):
        r = await client.request(method, path, headers=as_user(viewer), json={"status": "deleted"})
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert (await client.get(f"/v1/users/{uuid4()}", headers=as_user(viewer))).status_code == 404
    assert (await client.get("/v1/users/not-a-uuid", headers=as_user(viewer))).status_code == 422


async def test_suspended_user_hidden_and_read_only(
    client: httpx.AsyncClient, as_user: Auth, app: FastAPI
) -> None:
    uid = uuid4()
    await client.get("/v1/users/me", headers=as_user(uid))
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(update(User).where(User.id == uid).values(status=UserStatus.SUSPENDED))
    assert (await client.get(f"/v1/users/{uid}", headers=as_user(uuid4()))).status_code == 404
    r = await client.patch("/v1/users/me/profile", headers=as_user(uid), json={"bio": "hi"})
    assert r.status_code == 403


async def _wait_for(predicate: Callable[[], object], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():  # type: ignore[misc]
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


async def test_user_registered_event_is_consumed_idempotently(app: FastAPI) -> None:
    uid = uuid4()
    event = EventEnvelope(
        type=RoutingKeys.USER_REGISTERED,
        producer="auth-service",
        data={"user_id": str(uid), "provider": "email", "registered_at": "2026-01-01T00:00:00Z"},
    )
    broker = app.state.broker
    for _ in range(2):  # duplicate delivery
        await broker.publish(
            event.model_dump(mode="json"),
            exchange=EVENTS_EXCHANGE,
            routing_key=RoutingKeys.USER_REGISTERED,
            message_id=str(event.id),
        )

    async def processed() -> bool:
        async with app.state.db.sessionmaker() as s:
            return await s.get(ProcessedEvent, event.id) is not None

    await _wait_for(processed)
    await asyncio.sleep(0.5)
    async with app.state.db.sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(User).where(User.id == uid)) == 1
        assert (
            await s.scalar(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.event_id == event.id)
            )
            == 1
        )


async def test_poison_event_goes_to_dead_letter_queue(app: FastAPI) -> None:
    from cogo_common.events.topology import USER_SERVICE_USER_REGISTERED

    broker = app.state.broker
    bad = EventEnvelope(type=RoutingKeys.USER_REGISTERED, producer="x", data={"user_id": "nope"})
    await broker.publish(
        bad.model_dump(mode="json"),
        exchange=EVENTS_EXCHANGE,
        routing_key=RoutingKeys.USER_REGISTERED,
        message_id=str(bad.id),
    )
    dlq = await broker.declare_queue(USER_SERVICE_USER_REGISTERED.dead_letter_queue())

    async def dead_lettered() -> bool:
        message = await dlq.get(fail=False, no_ack=True)
        return message is not None and message.message_id == str(bad.id)

    await _wait_for(dead_lettered)
