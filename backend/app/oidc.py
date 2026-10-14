from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import urlopen

from jose import JWTError, jwt


OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
OIDC_JWKS_URL = os.getenv(
    "OIDC_JWKS_URL",
    f"{OIDC_ISSUER}/protocol/openid-connect/certs",
)
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "pfp-api")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "moneta")

JWKS_TTL_SECONDS = 300

_jwks_cache = {
    "keys": [],
    "expires_at": 0.0,
}
_jwks_lock = threading.Lock()


def _get_jwks(*, refresh: bool = False) -> list[dict]:
    """
    Fetch signing keys from the configured Keycloak realm.

    Keys are cached for 5 minutes. A missing kid triggers
    one refresh to support signing-key rotation.
    """
    if not OIDC_ISSUER:
        raise ValueError("OIDC_ISSUER is not configured")

    with _jwks_lock:
        now = time.monotonic()

        if refresh or now >= _jwks_cache["expires_at"]:
            url = OIDC_JWKS_URL

            with urlopen(url, timeout=3) as response:
                document = json.load(response)

            keys = document.get("keys")

            if not isinstance(keys, list):
                raise ValueError("Invalid JWKS document")

            _jwks_cache["keys"] = keys
            _jwks_cache["expires_at"] = (
                time.monotonic() + JWKS_TTL_SECONDS
            )

        return list(_jwks_cache["keys"])


def _find_signing_key(keys: list[dict], kid: str):
    for key in keys:
        if (
            key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("alg", "RS256") == "RS256"
            and key.get("use", "sig") == "sig"
        ):
            return key

    return None


def verify_oidc_access_token(token: str) -> dict | None:
    """
    Verify a Keycloak access token.

    Return verified issuer + subject, or None.

    Never trust claims or JWT headers before signature
    and claim validation completes.
    """
    if not OIDC_ISSUER:
        return None

    try:
        header = jwt.get_unverified_header(token)

        if header.get("alg") != "RS256":
            return None

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None

        key = _find_signing_key(_get_jwks(), kid)

        if key is None:
            key = _find_signing_key(
                _get_jwks(refresh=True),
                kid,
            )

        if key is None:
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=OIDC_AUDIENCE,
            issuer=OIDC_ISSUER,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
            },
        )

        if claims.get("typ") != "Bearer":
            return None

        if claims.get("azp") != OIDC_CLIENT_ID:
            return None

        subject = claims.get("sub")

        if not isinstance(subject, str) or not subject:
            return None

        return {
            "issuer": claims["iss"],
            "subject": subject,
        }

    except (JWTError, OSError, ValueError, TypeError):
        return None
