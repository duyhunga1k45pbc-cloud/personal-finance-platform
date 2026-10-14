import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.database import engine
from app.models import ExternalIdentity, User
from app.auth import hash_password

KEYCLOAK_URL = os.getenv("E2E_KEYCLOAK_URL", "http://keycloak:8080")
REALM = "pfp-e2e"
ISSUER = "http://localhost:18081/realms/pfp-e2e"


def get_subject():
    url = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token"

    body = urlencode({
        "grant_type": "password",
        "client_id": "moneta",
        "username": "e2e-user",
        "password": "e2e-test-password",
    }).encode()

    request = Request(url, data=body, method="POST")

    deadline = time.monotonic() + 180

    while True:
        try:
            with urlopen(request, timeout=10) as response:
                token = json.load(response)["access_token"]
            break
        except (HTTPError, URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Keycloak token endpoint not ready")
            time.sleep(3)

    # Decode only to discover the subject for test fixture setup.
    # This is never used to authorize requests.
    from jose import jwt
    claims = jwt.get_unverified_claims(token)

    assert claims["iss"] == ISSUER
    return claims["sub"]


def main():
    assert os.environ["DATABASE_URL"].endswith("/finance_e2e_db"), (
        "Refusing to seed a non-E2E database"
    )

    subject = get_subject()

    with Session(engine) as db:
        user = User(
            email="e2e@example.test",
            hashed_password=hash_password("e2e-only-local-password"),
        )
        db.add(user)
        db.flush()

        db.add(ExternalIdentity(
            issuer=ISSUER,
            subject=subject,
            user_id=user.id,
        ))

        db.commit()

    print("E2E identity mapping created")


if __name__ == "__main__":
    main()
