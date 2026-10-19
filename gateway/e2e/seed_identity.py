import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jose import jwt
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.database import engine
from app.models import ExternalIdentity, User

KEYCLOAK_URL = os.getenv(
    "E2E_KEYCLOAK_URL",
    "http://keycloak:8080",
)
REALM = "pfp-e2e"
ISSUER = "http://localhost:18081/realms/pfp-e2e"

USERS = (
    ("e2e-user", "e2e-test-password", "e2e@example.test"),
    ("e2e-user-b", "e2e-b-test-password", "e2e-b@example.test"),
)


def get_subject(username, password):
    url = (
        f"{KEYCLOAK_URL}/realms/{REALM}"
        "/protocol/openid-connect/token"
    )

    body = urlencode({
        "grant_type": "password",
        "client_id": "moneta",
        "username": username,
        "password": password,
    }).encode()

    deadline = time.monotonic() + 180

    while True:
        request = Request(url, data=body, method="POST")

        try:
            with urlopen(request, timeout=10) as response:
                token = json.load(response)["access_token"]
            break
        except (HTTPError, URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Keycloak token unavailable: {username}"
                )
            time.sleep(3)

    # Fixture discovery only; never used for authorization.
    claims = jwt.get_unverified_claims(token)
    assert claims["iss"] == ISSUER

    return claims["sub"]


def main():
    database_url = make_url(os.environ["DATABASE_URL"])

    assert database_url.database == "finance_e2e_db", (
        "Refusing to seed a non-E2E database"
    )

    subjects = [
        (get_subject(username, password), email)
        for username, password, email in USERS
    ]

    with Session(engine) as db:
        for subject, email in subjects:
            identity = (
                db.query(ExternalIdentity)
                .filter(
                    ExternalIdentity.issuer == ISSUER,
                    ExternalIdentity.subject == subject,
                )
                .one_or_none()
            )

            if identity is not None:
                user = db.get(User, identity.user_id)
                assert user is not None
                assert user.email == email, (
                    "Existing identity maps to unexpected user"
                )
                print(f"EXISTING: {email}")
                continue

            user = db.query(User).filter(
                User.email == email
            ).one_or_none()

            if user is None:
                user = User(
                    email=email,
                    hashed_password=hash_password(
                        "e2e-only-local-password"
                    ),
                )
                db.add(user)
                db.flush()

            db.add(ExternalIdentity(
                issuer=ISSUER,
                subject=subject,
                user_id=user.id,
            ))

            print(f"CREATED MAPPING: {email}")

        db.commit()

    print("E2E IDENTITY SEED: PASS")


if __name__ == "__main__":
    main()
