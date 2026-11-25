"""Add one synthetic OIDC owner mapping to a verified fresh live-E2E database."""

from __future__ import annotations

import os
import secrets

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.database import engine
from app.models import ExternalIdentity, User


def main() -> None:
    database_name = os.environ["PFP_E2E_DATABASE_NAME"]
    database_url = os.environ["DATABASE_URL"]
    parsed = make_url(database_url)
    if parsed.database != database_name or not database_name.startswith(
        "pfp_e2e_auto_"
    ):
        raise RuntimeError("Refusing to seed an unapproved E2E database")

    issuer = os.environ["OIDC_ISSUER"]
    subject = os.environ["PFP_E2E_TEST_SUBJECT"]
    email = os.environ["PFP_E2E_TEST_EMAIL"]

    with engine.begin() as connection:
        actual = connection.execute(text("SELECT current_database()")).scalar_one()
        if actual != database_name:
            raise RuntimeError("Connected database does not match E2E target")
        existing = connection.execute(
            text(
                "SELECT count(*) FROM external_identities "
                "WHERE issuer = :issuer AND subject = :subject"
            ),
            {"issuer": issuer, "subject": subject},
        ).scalar_one()
        email_exists = connection.execute(
            text("SELECT count(*) FROM users WHERE email = :email"),
            {"email": email},
        ).scalar_one()
        if existing or email_exists:
            raise RuntimeError("Synthetic owner identity already exists")

    with Session(engine) as session:
        user = User(
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
        )
        session.add(user)
        session.flush()
        session.add(
            ExternalIdentity(
                issuer=issuer,
                subject=subject,
                user_id=user.id,
            )
        )
        session.commit()
        print(f"PFP_E2E_OWNER_ID={user.id}")

    engine.dispose()


if __name__ == "__main__":
    main()
