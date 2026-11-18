import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from asgi_lifespan import LifespanManager
from cogo_common.auth import StaticKeyProvider, TokenVerifier
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from jwt.algorithms import OKPAlgorithm

from app.config import Settings
from app.main import create_app

SERVICE_DIR = Path(__file__).resolve().parents[1]
SIGNING_KEY = Ed25519PrivateKey.generate()
_JWK = {**json.loads(OKPAlgorithm.to_jwk(SIGNING_KEY.public_key())), "kid": "test", "alg": "EdDSA"}


def mint(user_id: UUID, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": "cogo-auth",
        "aud": "cogo-api",
        "sub": str(user_id),
        "sid": "s",
        "iat": now,
        "exp": now + 300,
        "jti": uuid4().hex,
    }
    claims.update(overrides)
    return jwt.encode(claims, SIGNING_KEY, algorithm="EdDSA", headers={"kid": "test"})


@pytest.fixture(scope="session")
def containers() -> Iterator[dict[str, str]]:
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.rabbitmq import RabbitMqContainer

    with (
        PostgresContainer("postgres:17-alpine", driver="asyncpg") as pg,
        RabbitMqContainer("rabbitmq:4-management-alpine") as rmq,
    ):
        rp = rmq.get_connection_params()
        yield {"db": pg.get_connection_url(), "amqp": f"amqp://guest:guest@{rp.host}:{rp.port}/"}


@pytest.fixture(scope="session")
def migrated(containers: dict[str, str]) -> dict[str, str]:
    env = {**os.environ, "DATABASE_URL": containers["db"]}
    subprocess.run(["alembic", "upgrade", "head"], cwd=SERVICE_DIR, env=env, check=True)
    return containers


@pytest.fixture(scope="session")
async def app(migrated: dict[str, str]) -> AsyncIterator[FastAPI]:
    containers = migrated
    settings = Settings(
        ENV="test",
        LOG_JSON=False,
        DATABASE_URL=containers["db"],
        RABBITMQ_URL=containers["amqp"],
        AUTH_JWKS_URL="http://127.0.0.1:9/unreachable",
    )
    application = create_app(settings)
    async with LifespanManager(application):
        application.state.token_verifier = TokenVerifier(
            StaticKeyProvider([jwt.PyJWK(_JWK)]), issuer="cogo-auth", audience="cogo-api"
        )
        yield application


@pytest.fixture(scope="session")
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://user"
    ) as c:
        yield c


@pytest.fixture
def as_user() -> Callable[[UUID], dict[str, str]]:
    return lambda user_id: {"Authorization": f"Bearer {mint(user_id)}"}
