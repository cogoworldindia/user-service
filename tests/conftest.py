import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import boto3
import httpx
import jwt
import pytest
from asgi_lifespan import LifespanManager
from botocore.config import Config
from cogo_common.auth import StaticKeyProvider, TokenVerifier
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from jwt.algorithms import OKPAlgorithm
from pydantic import SecretStr

from app.clients import Place
from app.config import Settings
from app.main import create_app

SERVICE_DIR = Path(__file__).resolve().parents[1]
SIGNING_KEY = Ed25519PrivateKey.generate()
_JWK = {**json.loads(OKPAlgorithm.to_jwk(SIGNING_KEY.public_key())), "kid": "test", "alg": "EdDSA"}
MEDIA_KEY, MEDIA_SECRET = "cogo", "cogo-secret-123"
OTHER_SERVICE_KEYS = {
    name: Ed25519PrivateKey.generate() for name in ("admin-service", "journey-service")
}


def service_headers(caller: str, admin_id: UUID | None = None) -> dict[str, str]:
    from cogo_common.auth.service_tokens import ServiceTokenSigner

    token = ServiceTokenSigner(caller, OTHER_SERVICE_KEYS[caller]).token_for("user-service")
    headers = {"Authorization": f"Bearer {token}"}
    if admin_id is not None:
        headers["X-Admin-Id"] = str(admin_id)
    return headers


def mint_reauth(user_id: UUID, purpose: str = "delete_account") -> str:
    now = int(time.time())
    claims = {
        "iss": "cogo-auth",
        "aud": "cogo-reauth",
        "sub": str(user_id),
        "sid": "s",
        "purpose": purpose,
        "iat": now,
        "exp": now + 300,
        "jti": uuid4().hex,
    }
    return jwt.encode(claims, SIGNING_KEY, algorithm="EdDSA", headers={"kid": "test"})


def mint(user_id: UUID) -> str:
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
    return jwt.encode(claims, SIGNING_KEY, algorithm="EdDSA", headers={"kid": "test"})


@pytest.fixture(scope="session")
def containers() -> Iterator[dict[str, str]]:
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.rabbitmq import RabbitMqContainer
    from testcontainers.core.container import DockerContainer

    s3 = (
        DockerContainer("rustfs/rustfs:1.0.6")
        .with_exposed_ports(9000)
        .with_env("RUSTFS_ACCESS_KEY", MEDIA_KEY)
        .with_env("RUSTFS_SECRET_KEY", MEDIA_SECRET)
    )
    with (
        PostgresContainer("postgres:17-alpine", driver="asyncpg") as pg,
        RabbitMqContainer("rabbitmq:4-management-alpine") as rmq,
        s3,
    ):
        endpoint = f"http://{s3.get_container_host_ip()}:{s3.get_exposed_port(9000)}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=MEDIA_KEY,
            aws_secret_access_key=MEDIA_SECRET,
            region_name="ap-south-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        for _ in range(40):
            try:
                client.create_bucket(Bucket="cogo-media")
                break
            except Exception:
                time.sleep(0.25)
        rp = rmq.get_connection_params()
        yield {
            "db": pg.get_connection_url(),
            "amqp": f"amqp://guest:guest@{rp.host}:{rp.port}/",
            "s3": endpoint,
        }


@pytest.fixture(scope="session")
def migrated(containers: dict[str, str]) -> dict[str, str]:
    env = {**os.environ, "DATABASE_URL": containers["db"]}
    subprocess.run(["alembic", "upgrade", "head"], cwd=SERVICE_DIR, env=env, check=True)
    return containers


@pytest.fixture(scope="session")
def service_keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("svc")
    (root / "public").mkdir()
    key = Ed25519PrivateKey.generate()
    (root / "user-service.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (root / "public" / "user-service.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    for name, other in OTHER_SERVICE_KEYS.items():
        (root / "public" / f"{name}.pem").write_bytes(
            other.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    return root / "user-service.pem", root / "public"


class FakePlaces:
    PLACES: ClassVar[dict[str, Place]] = {
        "pl_noida": Place("pl_noida", "Noida", "Uttar Pradesh", 28.5355, 77.3910)
    }

    async def resolve(self, place_id: str) -> Place:
        from cogo_common.errors import BadRequest

        if place_id not in self.PLACES:
            raise BadRequest("Unknown place", error_type="invalid_place")
        return self.PLACES[place_id]


class FakeChat:
    def __init__(self) -> None:
        self.result: dict[UUID, str] | None = {}

    async def connection_status(
        self, viewer_id: UUID, user_ids: list[UUID]
    ) -> dict[UUID, str] | None:
        return self.result


class FakeMailer:
    """Captures official-email codes instead of queuing them for notification-service."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_code(self, email: str, code: str, *, ttl_seconds: int) -> None:
        if self.fail:
            from cogo_common.errors import ServiceUnavailable

            raise ServiceUnavailable("Could not send the code right now, please retry")
        self.sent.append((email, code))

    def code_for(self, email: str) -> str:
        return next(code for to, code in reversed(self.sent) if to == email.lower())


class FakeSocial:
    def __init__(self) -> None:
        self.result: dict[UUID, bool] | None = {}

    async def follow_status(self, viewer_id: UUID, user_ids: list[UUID]) -> dict[UUID, bool] | None:
        return self.result


@pytest.fixture(scope="session")
async def app(migrated: dict[str, str], service_keys: tuple[Path, Path]) -> AsyncIterator[FastAPI]:
    settings = Settings(
        ENV="test",
        LOG_JSON=False,
        DATABASE_URL=migrated["db"],
        RABBITMQ_URL=migrated["amqp"],
        AUTH_JWKS_URL="http://127.0.0.1:9/unreachable",
        SERVICE_PRIVATE_KEY_FILE=service_keys[0],
        SERVICE_PUBLIC_KEYS_DIR=service_keys[1],
        MEDIA_S3_ENDPOINT=migrated["s3"],
        MEDIA_PUBLIC_BASE_URL=f"{migrated['s3']}/cogo-media",
        MEDIA_ACCESS_KEY=MEDIA_KEY,
        MEDIA_SECRET_KEY=MEDIA_SECRET,
        MEDIA_MAX_BYTES=10_000,
        KYC_OTP_HMAC_KEY=SecretStr("test-otp-key"),
        KYC_MOCK_WEBHOOK_SECRET=SecretStr("test-mock-secret"),
        KYC_PUBLIC_BASE_URL="http://user",
        KYC_MOCK_WEBHOOK_URL="http://user/v1/webhooks/kyc/mock",
        JOBS_ENABLED=False,  # tests run the jobs themselves
    )
    application = create_app(settings)
    async with LifespanManager(application):
        # the mock provider calls the webhook of this same app, in-process
        application.state.kyc_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://user"
        )
        application.state.kyc_mailer = FakeMailer()
        application.state.token_verifier = TokenVerifier(
            StaticKeyProvider([jwt.PyJWK(_JWK)]), issuer="cogo-auth", audience="cogo-api"
        )
        application.state.reauth_verifier = TokenVerifier(
            StaticKeyProvider([jwt.PyJWK(_JWK)]), issuer="cogo-auth", audience="cogo-reauth"
        )
        application.state.places = FakePlaces()
        application.state.social = FakeSocial()
        application.state.chat = FakeChat()
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


Any_ = Any
