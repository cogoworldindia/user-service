from functools import lru_cache

from cogo_common.settings import ServiceSettings


class Settings(ServiceSettings):
    SERVICE_NAME: str = "user-service"

    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    RABBITMQ_URL: str

    AUTH_JWKS_URL: str = "http://auth-service:8000/.well-known/jwks.json"
    JWT_ISSUER: str = "cogo-auth"
    JWT_AUDIENCE: str = "cogo-api"

    CONSUMERS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
