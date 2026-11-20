from app.config import Settings
from app.kyc.cashfree import CashfreeDigiLockerProvider
from app.kyc.providers import KycProvider, MockKycProvider


def build_provider(settings: Settings) -> KycProvider:
    """The configured provider (spec §9: real or mock, selected by env). Misconfiguration
    fails at startup, not on a user's first verification."""
    if settings.KYC_PROVIDER == "mock":
        if settings.ENV in ("production", "staging"):
            raise RuntimeError("KYC_PROVIDER=mock is for development and tests only")
        if settings.KYC_MOCK_WEBHOOK_SECRET is None:
            raise RuntimeError("KYC_MOCK_WEBHOOK_SECRET is required for the mock provider")
        return MockKycProvider(
            secret=settings.KYC_MOCK_WEBHOOK_SECRET.get_secret_value().encode(),
            public_base_url=settings.KYC_PUBLIC_BASE_URL,
        )
    if not settings.KYC_CASHFREE_CLIENT_ID or settings.KYC_CASHFREE_CLIENT_SECRET is None:
        raise RuntimeError("KYC_CASHFREE_CLIENT_ID and KYC_CASHFREE_CLIENT_SECRET are required")
    if not settings.KYC_RETURN_URL.startswith("https://"):
        raise RuntimeError("Cashfree needs an https KYC_RETURN_URL (an app link), not a scheme")
    key_file = settings.KYC_CASHFREE_PUBLIC_KEY_FILE
    return CashfreeDigiLockerProvider(
        base_url=settings.KYC_CASHFREE_BASE_URL,
        client_id=settings.KYC_CASHFREE_CLIENT_ID,
        client_secret=settings.KYC_CASHFREE_CLIENT_SECRET.get_secret_value(),
        documents=settings.KYC_CASHFREE_DOCUMENTS,
        public_key_pem=key_file.read_bytes() if key_file else None,
    )
