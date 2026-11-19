from functools import lru_cache
from pathlib import Path

from cogo_common.settings import ServiceSettings


class Settings(ServiceSettings):
    SERVICE_NAME: str = "user-service"

    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    RABBITMQ_URL: str
    REDIS_URL: str | None = None  # app config overrides (admin-service); optional

    AUTH_JWKS_URL: str = "http://auth-service:8000/.well-known/jwks.json"
    JWT_ISSUER: str = "cogo-auth"
    JWT_AUDIENCE: str = "cogo-api"

    # Service-to-service auth (spec §1A.4)
    SERVICE_PRIVATE_KEY_FILE: Path = Path("/run/secrets/service_key")
    SERVICE_PUBLIC_KEYS_DIR: Path = Path("/run/service-keys")
    JOURNEY_SERVICE_URL: str = "http://journey-service:8000"
    SOCIAL_SERVICE_URL: str = "http://social-service:8000"

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

    CONSUMERS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
