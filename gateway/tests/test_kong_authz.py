import base64
import json
import os
import re
import socket
import subprocess
import time
import uuid
import threading

from mock_authz import start_mock_server
from pathlib import Path

import pytest
import requests


ROOT = Path(__file__).resolve().parents[2]
KONG_CONFIG = ROOT / "gateway/kong/kong-authz-test.yml"
SECRET_FILE = ROOT / "backend/.local/gateway-authz-secret"
IMAGE = "pfp-kong-authz:0.2.0"
BASE_URL = "http://127.0.0.1:8002"
PORT = 8002


def run(*args, **kwargs):
    return subprocess.run(
        args,
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_jwt(private_key, *, tamper=False, subject="unavailable"):
    now = int(time.time())

    header = {
        "alg": "RS256",
        "typ": "JWT",
        "kid": "pfp-integration-test",
    }
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + 300,
    }

    message = ".".join(
        b64(json.dumps(obj, separators=(",", ":")).encode())
        for obj in (header, payload)
    )

    signature = subprocess.run(
        [
            "openssl", "dgst", "-sha256",
            "-sign", str(private_key),
        ],
        input=message.encode(),
        capture_output=True,
        check=True,
    ).stdout

    token = message + "." + b64(signature)

    if tamper:
        parts = token.split(".")
        sig = parts[2]
        parts[2] = ("A" if sig[0] != "A" else "B") + sig[1:]
        token = ".".join(parts)

    return token


@pytest.fixture(scope="module")
def kong_test(tmp_path_factory):
    temp = tmp_path_factory.mktemp("kong_authz")
    os.chmod(temp, 0o700)

    private_key = temp / "private.pem"
    public_key = temp / "public.pem"
    config_file = temp / "kong.yml"

    run(
        "openssl", "genpkey",
        "-algorithm", "RSA",
        "-pkeyopt", "rsa_keygen_bits:2048",
        "-out", str(private_key),
    )
    os.chmod(private_key, 0o600)

    run(
        "openssl", "pkey",
        "-in", str(private_key),
        "-pubout",
        "-out", str(public_key),
    )

    config = KONG_CONFIG.read_text()

    endpoint = (
        "http://host.docker.internal:8765"
        "/internal/gateway/authorize"
    )
    assert endpoint in config
    assert "pfp-authz" in config
    assert "algorithm: RS256" in config

    # Simulate unavailable authorization service.
    config = config.replace(
        endpoint,
        "http://host.docker.internal:18999"
        "/internal/gateway/authorize",
    )

    # Use mock as the upstream for allowed requests too.
    config = config.replace(
        "url: http://host.docker.internal:8765",
        "url: http://host.docker.internal:18999",
    )

    config = config.replace(
        "{vault://env/pfp-gateway-authz-secret}",
        "{vault://env/gateway-authz-secret}",
    )

    assert "{vault://env/gateway-authz-secret}" in config

    public_lines = "".join(
        "          " + line + "\n"
        for line in public_key.read_text().splitlines()
    )

    config, count = re.subn(
        r"(        rsa_public_key: \|\n)(?:          .*\n)+",
        lambda match: match.group(1) + public_lines,
        config,
        count=1,
    )
    assert count == 1

    config, count = re.subn(
        r'key: "[^"]+"',
        'key: "pfp-integration-test"',
        config,
        count=1,
    )
    assert count == 1

    config_file.write_text(config)
    os.chmod(config_file, 0o600)

    secret = SECRET_FILE.read_text().strip()
    assert secret, "Gateway secret is empty"

    env = os.environ.copy()
    env["PFP_GATEWAY_AUTHZ_SECRET"] = secret

    container = "pfp-kong-pytest-" + uuid.uuid4().hex[:12]
    started = False

    server = start_mock_server()
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    server_thread.start()

    try:
        # Do not disrupt another service on port 8002.
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", PORT)) == 0:
                pytest.fail("Port 8002 is already in use")

        run(
            "docker", "run", "-d",
            "--name", container,
            "--memory", "512m",
            "--cpus", "1",
            "--add-host=host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{PORT}:8000",
            "-e", "KONG_DATABASE=off",
            "-e", "KONG_VAULT_ENV_PREFIX=PFP_",
            "-e", "PFP_GATEWAY_AUTHZ_SECRET",
            "-e", "KONG_PROXY_LISTEN=0.0.0.0:8000",
            "-e", "KONG_NGINX_WORKER_PROCESSES=2",
            "-e", "KONG_DECLARATIVE_CONFIG=/etc/kong/kong.yml",
            "-v", f"{config_file}:/etc/kong/kong.yml:ro",
            IMAGE,
            env=env,
        )
        started = True

        deadline = time.monotonic() + 60

        while time.monotonic() < deadline:
            status = run(
                "docker", "inspect", container,
                "--format",
                "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            ).stdout.strip()

            if status == "running healthy":
                break

            if status.startswith("exited"):
                pytest.fail("Kong container exited during startup")

            time.sleep(2)
        else:
            pytest.fail("Kong did not become healthy within 60s")

        yield private_key

    finally:
        if started:
            result = subprocess.run(
                ["docker", "logs", "--tail", "30", container],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                print("\nKong test logs:\n", result.stdout[-3000:])

        try:
            subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True,
                timeout=20,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


def test_missing_jwt(kong_test):
    response = requests.get(
        BASE_URL + "/accounts",
        timeout=5,
    )
    assert response.status_code == 401


def test_invalid_signature(kong_test):
    token = generate_jwt(kong_test, tamper=True)

    response = requests.get(
        BASE_URL + "/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert response.status_code == 401


def test_authorization_service_unavailable(kong_test):
    token = generate_jwt(kong_test)

    response = requests.get(
        BASE_URL + "/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )

    assert response.status_code == 503
    assert response.json()["message"] == "Authorization unavailable"



def test_authorization_allow(kong_test):
    token = generate_jwt(kong_test, subject="allow")
    response = requests.get(
        BASE_URL + "/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert response.status_code == 200
    assert response.json() == {"accounts": []}


def test_authorization_deny(kong_test):
    token = generate_jwt(kong_test, subject="deny")
    response = requests.post(
        BASE_URL + "/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert response.status_code == 403
    assert response.json()["message"] == "Access denied"


def test_authorization_timeout(kong_test):
    token = generate_jwt(kong_test, subject="slow")
    start = time.monotonic()

    response = requests.get(
        BASE_URL + "/accounts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )

    elapsed = time.monotonic() - start

    assert response.status_code == 503
    assert response.json()["message"] == "Authorization unavailable"
    assert elapsed < 1.9, f"Timeout was too slow: {elapsed:.2f}s"
