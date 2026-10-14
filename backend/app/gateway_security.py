import os
import secrets

from fastapi import Header, HTTPException


def verify_gateway(
    x_gateway_secret: str | None = Header(default=None),
) -> None:
    expected = os.environ.get("PFP_GATEWAY_AUTHZ_SECRET")

    if (
        not expected
        or not x_gateway_secret
        or not secrets.compare_digest(
            x_gateway_secret.encode(),
            expected.encode(),
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )
