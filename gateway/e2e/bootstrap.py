import json
import os
import re
import time
from pathlib import Path
from urllib.request import urlopen

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parent
JWKS_URL = os.getenv(
    "E2E_JWKS_URL",
    "http://127.0.0.1:18081/realms/pfp-e2e/protocol/openid-connect/certs",
)


def fetch_signing_key():
    deadline = time.monotonic() + 120

    while time.monotonic() < deadline:
        try:
            with urlopen(JWKS_URL, timeout=5) as response:
                keys = json.load(response)["keys"]

            for key in keys:
                if (
                    key.get("kty") == "RSA"
                    and key.get("use") == "sig"
                    and key.get("alg") == "RS256"
                    and key.get("kid")
                ):
                    return key
        except (OSError, KeyError, ValueError):
            pass

        time.sleep(3)

    raise RuntimeError("Keycloak JWKS not ready after 120 seconds")


def main():
    key = fetch_signing_key()

    public_key = rsa.RSAPublicNumbers(
        e=int.from_bytes(
            __import__("base64").urlsafe_b64decode(
                key["e"] + "=" * (-len(key["e"]) % 4)
            ),
            "big",
        ),
        n=int.from_bytes(
            __import__("base64").urlsafe_b64decode(
                key["n"] + "=" * (-len(key["n"]) % 4)
            ),
            "big",
        ),
    ).public_key()

    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    config = (ROOT / "kong-template.yml").read_text()

    config, count = re.subn(
        r'key: "[^"]+"',
        lambda _: 'key: "' + key["kid"] + '"',
        config,
        count=1,
    )
    assert count == 1

    public_lines = "".join(
        "          " + line + "\n"
        for line in pem.splitlines()
    )

    config, count = re.subn(
        r"(        rsa_public_key: \|\n)(?:          .*\n)+",
        lambda match: match.group(1) + public_lines,
        config,
        count=1,
    )
    assert count == 1

    output = ROOT / "generated" / "kong-e2e.yml"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(config)

    print("Generated Kong E2E config from Keycloak signing key")


if __name__ == "__main__":
    main()
