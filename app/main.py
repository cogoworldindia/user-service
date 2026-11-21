from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from cogo_common.app import create_service_app
from cogo_common.appconfig import AppConfig
from cogo_common.auth import JWKSClient, TokenVerifier
from cogo_common.auth.service_tokens import build_service_auth
from cogo_common.db import Database
from cogo_common.events.broker import broker_ready, create_broker, start_broker
from cogo_common.events.outbox import OutboxRelay, rabbit_publisher
from cogo_common.events.topology import declare_topology
from cogo_common.http import create_http_client
from cogo_common.internal_http import InternalClient
from cogo_common.maintenance import from_settings as maintenance_from_settings
from fastapi import FastAPI

from app.api import router
from app.api_internal import router as internal_router
from app.clients import ChatClient, PlacesClient, SocialClient
from app.config import Settings, get_settings
from app.consumers import register_consumers
from app.deletion import DeletionTracker
from app.jobs import Jobs
from app.kyc.api import router as kyc_router
from app.kyc.factory import build_provider
from app.kyc.mailer import KycMailer
from app.kyc.mock_pages import router as kyc_mock_router
from app.kyc.providers import MockKycProvider
from app.kyc.service import OtpPolicy
from app.media import MediaStore
from app.moderation import GoogleVisionModerator, ImageModerator, MockImageModerator


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        prepared_statements=settings.DB_PREPARED_STATEMENTS,
    )
    broker = create_broker(settings.RABBITMQ_URL, service_name=settings.SERVICE_NAME)
    config = AppConfig.from_settings(settings)
    kyc_provider = build_provider(settings)
    media = MediaStore(settings)
    moderator = build_moderator(settings)
    if settings.CONSUMERS_ENABLED:
        register_consumers(broker, db.sessionmaker, media=media, moderator=moderator)
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
        app.state.reauth_verifier = TokenVerifier(
            jwks, issuer=settings.JWT_ISSUER, audience=settings.REAUTH_AUDIENCE
        )
        journey = InternalClient(
            settings.JOURNEY_SERVICE_URL, audience="journey-service", signer=signer
        )
        social = InternalClient(
            settings.SOCIAL_SERVICE_URL, audience="social-service", signer=signer
        )
        app.state.places = PlacesClient(journey)
        app.state.social = SocialClient(social)
        chat = InternalClient(settings.CHAT_SERVICE_URL, audience="chat-service", signer=signer)
        app.state.chat = ChatClient(chat)
        app.state.kyc_http = create_http_client(timeout=10.0)  # the mock calls the webhook
        participants = {  # spec §7: who has to finish a deletion
            "auth": InternalClient(
                settings.AUTH_SERVICE_URL, audience="auth-service", signer=signer
            ),
            "journey": journey,
            "social": social,
            "chat": chat,
            "notification": InternalClient(
                settings.NOTIFICATION_SERVICE_URL, audience="notification-service", signer=signer
            ),
        }
        app.state.deletion_tracker = DeletionTracker(
            db.sessionmaker,
            participants,
            alert_after=timedelta(hours=settings.DELETION_ALERT_HOURS),
        )
        jobs = Jobs(
            db.sessionmaker, app.state.deletion_tracker, interval=settings.JOBS_INTERVAL_SECONDS
        )
        await jwks.prefetch()
        if settings.MEDIA_DEV_BOOTSTRAP:
            await app.state.media.dev_bootstrap()
        await start_broker(broker, declare=declare_topology)
        relay = OutboxRelay(db.sessionmaker, rabbit_publisher(broker))
        relay.start()
        maintenance = maintenance_from_settings(
            settings,
            db.sessionmaker,
            service=settings.SERVICE_NAME,
            has_inbox=True,
            has_outbox=True,
        )
        maintenance.start(purge=settings.MAINTENANCE_ENABLED)
        config.start()
        if settings.JOBS_ENABLED:
            jobs.start()
        try:
            yield
        finally:
            await jobs.stop()
            await maintenance.stop()
            await relay.stop()
            await broker.stop()
            for client in participants.values():
                await client.aclose()
            await app.state.kyc_http.aclose()
            await kyc_provider.aclose()
            await moderator.aclose()
            await config.aclose()
            await http.aclose()
            await db.dispose()

    app = create_service_app(
        settings,
        title="CoGo User Service",
        lifespan=lifespan,
        engine=db.engine,
        health_checks={"postgres": db.ping, "rabbitmq": lambda: broker_ready(broker)},
        max_body_size=64 * 1024,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.broker = broker
    app.state.media = media
    app.state.service_verifier = service_verifier
    app.state.config = config
    app.state.kyc_provider = kyc_provider
    app.state.kyc_mailer = KycMailer(broker, producer=settings.SERVICE_NAME)
    app.state.kyc_otp = OtpPolicy(
        hmac_key=settings.KYC_OTP_HMAC_KEY.get_secret_value().encode(),
        ttl_seconds=settings.KYC_OTP_TTL_SECONDS,
        max_attempts=settings.KYC_OTP_MAX_ATTEMPTS,
        sends_per_hour=settings.KYC_OTP_SENDS_PER_HOUR,
    )
    app.include_router(router)
    app.include_router(kyc_router)
    app.include_router(internal_router)
    if isinstance(kyc_provider, MockKycProvider):
        app.include_router(kyc_mock_router)
    return app


def build_moderator(settings: Settings) -> ImageModerator:
    if settings.IMAGE_MODERATION_PROVIDER == "google":
        if settings.GOOGLE_VISION_API_KEY is None:
            raise RuntimeError(
                "GOOGLE_VISION_API_KEY is required for IMAGE_MODERATION_PROVIDER=google"
            )
        return GoogleVisionModerator(settings.GOOGLE_VISION_API_KEY.get_secret_value())
    return MockImageModerator()
