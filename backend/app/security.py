from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_ALLOWED_JWT_ALGORITHMS = {"HS256"}
_INSECURE_SECRET_KEYS = {
    "",
    "your-secret-key-change-this-later",
    "change-me",
    "changeme",
    "secret",
}


class SecurityConfigurationError(RuntimeError):
    """Raised when security-sensitive runtime configuration is unsafe."""


@dataclass(frozen=True)
class SecuritySettings:
    app_env: str
    secret_key: str
    jwt_algorithm: str
    jwt_issuer: str
    jwt_audience: str
    access_token_expire_minutes: int

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def validate_security_values(
    *,
    app_env: str,
    secret_key: str | None,
    jwt_algorithm: str,
    access_token_expire_minutes: int,
) -> None:
    normalized_env = app_env.strip().lower()
    normalized_algorithm = jwt_algorithm.strip().upper()

    if normalized_algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise SecurityConfigurationError(
            "JWT algorithm is not allowed. V1 supports HS256 only."
        )

    if access_token_expire_minutes <= 0 or access_token_expire_minutes > 24 * 60:
        raise SecurityConfigurationError(
            "ACCESS_TOKEN_EXPIRE_MINUTES must be between 1 and 1440."
        )

    if normalized_env == "production":
        candidate = (secret_key or "").strip()
        if candidate.lower() in _INSECURE_SECRET_KEYS or len(candidate) < 32:
            raise SecurityConfigurationError(
                "Production SECRET_KEY must be explicitly configured and at least 32 characters."
            )


def load_security_settings() -> SecuritySettings:
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    jwt_algorithm = os.getenv(
        "JWT_ALGORITHM",
        os.getenv("ALGORITHM", "HS256"),
    ).strip().upper()
    access_token_expire_minutes = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    )
    configured_secret = os.getenv("SECRET_KEY")

    validate_security_values(
        app_env=app_env,
        secret_key=configured_secret,
        jwt_algorithm=jwt_algorithm,
        access_token_expire_minutes=access_token_expire_minutes,
    )

    # Development/test may use a deterministic local-only key so the app remains
    # easy to run. Production is fail-closed above and can never use this value.
    secret_key = configured_secret or (
        "dev-only-personal-finance-secret-key-not-for-production-2026"
    )

    return SecuritySettings(
        app_env=app_env,
        secret_key=secret_key,
        jwt_algorithm=jwt_algorithm,
        jwt_issuer=os.getenv("JWT_ISSUER", "personal-finance-api"),
        jwt_audience=os.getenv("JWT_AUDIENCE", "personal-finance-client"),
        access_token_expire_minutes=access_token_expire_minutes,
    )


settings = load_security_settings()
