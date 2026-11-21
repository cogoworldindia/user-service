from functools import lru_cache
from pathlib import Path
from typing import Literal

from cogo_common.settings import ServiceSettings
from pydantic import Field, SecretStr


class Settings(ServiceSettings):
    SERVICE_NAME: str = "user-service"

    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    RABBITMQ_URL: str

    AUTH_JWKS_URL: str = "http://auth-service:8000/.well-known/jwks.json"
    JWT_ISSUER: str = "cogo-auth"
    JWT_AUDIENCE: str = "cogo-api"

    # Service-to-service auth (spec §1A.4)
    SERVICE_PRIVATE_KEY_FILE: Path = Path("/run/secrets/service_key")
    SERVICE_PUBLIC_KEYS_DIR: Path = Path("/run/service-keys")
    JOURNEY_SERVICE_URL: str = "http://journey-service:8000"
    SOCIAL_SERVICE_URL: str = "http://social-service:8000"
    CHAT_SERVICE_URL: str = "http://chat-service:8000"
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    NOTIFICATION_SERVICE_URL: str = "http://notification-service:8000"
    # Account deletion needs a fresh OTP re-confirm: a short token auth-service issues for it
    REAUTH_AUDIENCE: str = "cogo-reauth"

    # Media (S3-compatible: MinIO in dev, S3/GCS in prod)
    MEDIA_BUCKET: str = "cogo-media"
    MEDIA_S3_ENDPOINT: str | None = None  # internal endpoint used by the service
    MEDIA_PRESIGN_ENDPOINT: str | None = None  # endpoint clients upload to (signed host)
    MEDIA_PUBLIC_BASE_URL: str = "http://localhost:9000/cogo-media"  # CDN base
    MEDIA_ACCESS_KEY: str | None = None
    MEDIA_SECRET_KEY: str | None = None
    MEDIA_REGION: str = "ap-south-1"
    MEDIA_MAX_BYTES: int = 5 * 1024 * 1024
    MEDIA_DEV_BOOTSTRAP: bool = False

    # eKYC (spec §5.9, §9). The mock provider serves its own pages and is refused in production.
    KYC_PROVIDER: Literal["mock", "cashfree"] = "mock"
    KYC_OTP_HMAC_KEY: SecretStr  # official-email codes are stored only as HMACs
    KYC_OTP_TTL_SECONDS: int = 600
    KYC_OTP_MAX_ATTEMPTS: int = 5
    KYC_OTP_SENDS_PER_HOUR: int = 3
    KYC_GOV_ID_SESSION_MINUTES: int = 30  # an unfinished ID session counts as expired after this
    KYC_RETURN_URL: str = "cogo://kyc/return"  # app deep link; `?attempt_id=` is appended
    KYC_PUBLIC_BASE_URL: str = "http://localhost:8000"  # public origin of the mock's pages
    KYC_MOCK_WEBHOOK_SECRET: SecretStr | None = None
    KYC_MOCK_WEBHOOK_URL: str = "http://127.0.0.1:8000/v1/webhooks/kyc/mock"
    # Cashfree Secure ID (DigiLocker). Its webhook URL is set in the Cashfree dashboard:
    # https://<public host>/v1/webhooks/kyc/cashfree
    KYC_CASHFREE_BASE_URL: str = "https://sandbox.cashfree.com/verification"
    KYC_CASHFREE_CLIENT_ID: str | None = None
    KYC_CASHFREE_CLIENT_SECRET: SecretStr | None = None
    KYC_CASHFREE_PUBLIC_KEY_FILE: Path | None = None  # x-cf-signature instead of IP allow-list
    KYC_CASHFREE_DOCUMENTS: list[str] = Field(default_factory=lambda: ["AADHAAR"])  # or PAN, ...

    # Image moderation (spec §5.10): Cloud Vision SafeSearch, or a mock in development
    IMAGE_MODERATION_PROVIDER: Literal["mock", "google"] = "mock"
    GOOGLE_VISION_API_KEY: SecretStr | None = None

    # Periodic jobs: suspensions that run out, the account-deletion tracker
    JOBS_ENABLED: bool = True
    JOBS_INTERVAL_SECONDS: float = 60.0
    DELETION_ALERT_HOURS: float = 24.0

    CONSUMERS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
