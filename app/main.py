from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cogo_common.app import create_service_app
from cogo_common.auth import JWKSClient, TokenVerifier
from cogo_common.auth.service_tokens import build_service_auth
from cogo_common.db import Database
from cogo_common.events.broker import create_broker
from cogo_common.events.outbox import OutboxRelay, rabbit_publisher
from cogo_common.events.topology import declare_topology
from cogo_common.http import create_http_client
from cogo_common.internal_http import InternalClient
from fastapi import FastAPI

from app.api import router
from app.clients import PlacesClient, SocialClient
from app.config import Settings, get_settings
from app.consumers import register_consumers
from app.media import MediaStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
    )
    broker = create_broker(settings.RABBITMQ_URL, service_name=settings.SERVICE_NAME)
    if settings.CONSUMERS_ENABLED:
        register_consumers(broker, db.sessionmaker)
    signer, service_verifier = build_service_auth(
        settings.SERVICE_NAME, settings.SERVICE_PRIVATE_KEY_FILE, settings.SERVICE_PUBLIC_KEYS_DIR
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = create_http_client(timeout=5.0)
        jwks = JWKSClient(settings.AUTH_JWKS_URL, http=http)
        app.state.token_verifier = TokenVerifier(
            jwks, issuer=settings.JWT_ISSUER, audience=settings.JWT_AUDIENCE
        )
        journey = InternalClient(
            settings.JOURNEY_SERVICE_URL, audience="journey-service", signer=signer
        )
        social = InternalClient(
            settings.SOCIAL_SERVICE_URL, audience="social-service", signer=signer
        )
        app.state.places = PlacesClient(journey)
        app.state.social = SocialClient(social)
        await jwks.prefetch()
        if settings.MEDIA_DEV_BOOTSTRAP:
            await app.state.media.dev_bootstrap()
        await broker.connect()
        await declare_topology(broker)
        await broker.start()
        relay = OutboxRelay(db.sessionmaker, rabbit_publisher(broker))
        relay.start()
        try:
            yield
        finally:
            await relay.stop()
            await broker.stop()
            await journey.aclose()
            await social.aclose()
            await http.aclose()
            await db.dispose()

    app = create_service_app(
        settings,
        title="CoGo User Service",
        lifespan=lifespan,
        engine=db.engine,
        health_checks={"postgres": db.ping, "rabbitmq": lambda: broker.ping(timeout=2.0)},
        max_body_size=64 * 1024,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.broker = broker
    app.state.media = MediaStore(settings)
    app.state.service_verifier = service_verifier
    app.include_router(router)
    return app
