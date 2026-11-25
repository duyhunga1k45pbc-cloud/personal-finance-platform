#!/usr/bin/env python3
"""Run the real browser Financial Insights flow in a disposable Compose project."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
E2E = ROOT / "gateway" / "e2e"
LIVE_COMPOSE = E2E / "compose.live.override.yml"
KONG_TEMPLATE = ROOT / "gateway" / "kong" / "kong-http.yml"
REALM_FILE = E2E / "realm.json"
SEED_SCRIPT = E2E / "seed_live_owner.py"

FORBIDDEN_DATABASES = {
    "finance_db",
    "finance_test",
    "finance_smoke_test",
    "pfp_smoke_test",
    "finance_demo_db",
}
DATABASE_NAME = re.compile(r"^pfp_e2e_auto_[a-z0-9_]{1,40}$")
TRACKED_INPUTS = (
    "ai-service/Dockerfile",
    "ai-service/go.mod",
    "ai-service/cmd/server/main.go",
    "ai-service/internal/analysis/analysis.go",
    "ai-service/internal/analysis/analysis_test.go",
    "ai-service/internal/analysis/model_output.schema.json",
    "ai-service/cmd/server/main_test.go",
    "backend/Dockerfile",
    "backend/app/analysis_context.py",
    "backend/app/main.py",
    "backend/app/routers/analytics.py",
    "backend/tests/test_analytics_context_integration.py",
    "backend/tests/test_analytics_context_unit.py",
    "backend/tests/test_analytics_insights.py",
    "frontend/.env.integration.example",
    "frontend/e2e/insights.spec.ts",
    "frontend/e2e/live-financial-insights.spec.ts",
    "frontend/e2e/support/keycloak.ts",
    "frontend/package-lock.json",
    "frontend/package.json",
    "frontend/playwright.live.config.ts",
    "frontend/src/api/insights.ts",
    "frontend/src/auth/keycloak.ts",
    "frontend/src/features/insights/FinancialInsightsPage.tsx",
    "frontend/vite.config.ts",
    "gateway/e2e/compose.live.override.yml",
    "gateway/e2e/README.live.md",
    "gateway/e2e/realm.json",
    "gateway/e2e/run_live_e2e.py",
    "gateway/e2e/seed_live_owner.py",
    "gateway/kong/kong-http.yml",
)
JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]+")
URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@/\s]+(@)"
)
SECRET_FIELD_PATTERN = re.compile(
    r'(?i)("?(?:access_token|refresh_token|client_secret|password|secret|authorization)"?\s*[:=]\s*"?)[^\s,"}]+'
)


class RunnerError(RuntimeError):
    pass


def sanitize(text: str) -> str:
    text = URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]\2", text)
    text = BEARER_PATTERN.sub(r"\1[REDACTED]", text)
    text = JWT_PATTERN.sub("[JWT REDACTED]", text)
    return SECRET_FIELD_PATTERN.sub(r"\1[REDACTED]", text)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        safe = sanitize(result.stdout)
        if label:
            print(f"{label} failed (exit {result.returncode}):")
        if safe:
            print(safe[-6000:])
        raise RunnerError(label or f"Command failed with exit {result.returncode}")
    return result


def compose_args(project: str, env_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        project,
        "-f",
        str(LIVE_COMPOSE),
    ]


def parse_url(value: str):
    parts = urlsplit(value)
    if not parts.scheme.startswith("postgres") or not parts.hostname:
        raise RunnerError("E2E DATABASE_URL is not a PostgreSQL URL")
    return parts


def validate_database_name(database: str) -> None:
    if not DATABASE_NAME.fullmatch(database) or database in FORBIDDEN_DATABASES:
        raise RunnerError("Generated test database name failed its safety check")


def validate_database_target(
    value: str, *, database: str, user: str
) -> None:
    target = parse_url(value)
    if (
        target.hostname != "postgres"
        or target.port != 5432
        or target.path.lstrip("/") != database
        or target.username != user
    ):
        raise RunnerError(
            "Effective FastAPI/Alembic URL is not the generated test database"
        )


def validate_database_identity(
    identity: str, *, database: str, user: str
) -> None:
    if identity != f"{database}|{user}":
        raise RunnerError("Connected PostgreSQL identity differs from the E2E target")


def validate_alembic_revision(current: str, heads: str) -> None:
    head_ids = set(
        re.findall(r"^([A-Za-z0-9_.-]+)\s+\(head\)", heads, re.MULTILINE)
    )
    if not head_ids or not all(
        re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(head)}(?![A-Za-z0-9_.-])",
            current,
        )
        for head in head_ids
    ):
        raise RunnerError("New disposable database is not at the source Alembic head")


def make_database_url(user: str, password: str, database: str) -> str:
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@postgres:5432/{database}"
    )


def write_compose_env(path: Path, values: dict[str, str]) -> None:
    # URL quoting and urlsafe random secrets prevent Compose interpolation.
    content = "".join(f"{key}={value.replace('$', '$$')}\n" for key, value in values.items())
    path.write_text(content)
    path.chmod(0o600)


def verify_tracked_inputs() -> None:
    result = run(
        ["git", "ls-files", "--error-unmatch", *TRACKED_INPUTS],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        missing = sorted(set(TRACKED_INPUTS) - set(result.stdout.splitlines()))
        raise RunnerError(
            "Required E2E source/configuration paths are not tracked: "
            + ", ".join(missing)
        )


def allocate_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_runtime_realm(path: Path, web_origin: str) -> None:
    realm = json.loads(REALM_FILE.read_text())
    clients = [client for client in realm.get("clients", []) if client.get("clientId") == "moneta"]
    if len(clients) != 1:
        raise RunnerError("The E2E realm must contain exactly one moneta client")
    clients[0]["redirectUris"] = [f"{web_origin}/*"]
    clients[0]["webOrigins"] = [web_origin]
    clients[0].setdefault("attributes", {})["pkce.code.challenge.method"] = "S256"
    realm["users"] = []
    path.write_text(json.dumps(realm, indent=2) + "\n")
    path.chmod(0o644)


def compose_project_resources(project: str) -> dict[str, list[str]]:
    resources = {
        "containers": ["docker", "container", "ls", "-aq"],
        "networks": ["docker", "network", "ls", "-q"],
        "volumes": ["docker", "volume", "ls", "-q"],
    }
    found = {}
    for kind, command in resources.items():
        result = run(
            command + ["--filter", f"label=com.docker.compose.project={project}"],
            check=False,
        )
        if result.returncode:
            raise RunnerError(f"Unable to inspect run-scoped Docker {kind}")
        found[kind] = [item for item in result.stdout.splitlines() if item]
    return found


def assert_no_project_resources(project: str) -> None:
    resources = compose_project_resources(project)
    remaining = [name for names in resources.values() for name in names]
    if remaining:
        raise RunnerError(
            "Run-scoped Docker resources remain after cleanup: "
            + ", ".join(remaining)
        )


def postgres_exec(compose: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    return run(
        compose + ["exec", "-T", "postgres", *args],
        check=False,
    )


def identity_admin_token(base_url: str, password: str) -> str:
    body = (
        "grant_type=password&client_id=admin-cli&username=admin&password="
        + quote(password, safe="")
    ).encode()
    request = Request(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        return json.load(response)["access_token"]


def admin_json(base_url: str, token: str, path: str, method: str = "GET", body: Any = None):
    encoded = None if body is None else json.dumps(body).encode()
    request = Request(
        f"{base_url}{path}",
        data=encoded,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read()
            return response.status, response.headers, json.loads(payload) if payload else None
    except HTTPError as error:
        raise RunnerError(f"Keycloak admin API returned HTTP {error.code}") from None


def validate_live_client(base_url: str, token: str, web_origin: str) -> None:
    status, _, clients = admin_json(
        base_url,
        token,
        "/admin/realms/pfp-e2e/clients?clientId=moneta",
    )
    if status != 200 or not clients or len(clients) != 1:
        raise RunnerError("The live Keycloak moneta client is missing or ambiguous")
    client = clients[0]
    if client.get("publicClient") is not True or client.get("standardFlowEnabled") is not True:
        raise RunnerError("Keycloak moneta must be a public Authorization Code client")
    if client.get("attributes", {}).get("pkce.code.challenge.method") != "S256":
        raise RunnerError("Keycloak moneta does not enforce PKCE S256")
    if f"{web_origin}/*" not in client.get("redirectUris", []):
        raise RunnerError("Keycloak moneta is missing the configured local redirect URI")
    if web_origin not in client.get("webOrigins", []):
        raise RunnerError("Keycloak moneta is missing the configured local web origin")
    mappers = client.get("protocolMappers", [])
    if not any(
        item.get("protocolMapper") == "oidc-audience-mapper"
        and item.get("config", {}).get("included.client.audience") == "pfp-api"
        and item.get("config", {}).get("access.token.claim") == "true"
        for item in mappers
    ):
        raise RunnerError("Keycloak moneta lacks the required pfp-api access-token audience")


def wait_http(url: str, *, expected: int = 200, timeout: float = 120) -> bytes:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers={"Cache-Control": "no-cache"})
            with urlopen(request, timeout=4) as response:
                payload = response.read()
                if response.status == expected:
                    return payload
                last = RuntimeError(f"unexpected HTTP {response.status}")
        except (OSError, URLError, HTTPError, TimeoutError) as error:
            last = error
        time.sleep(0.4)
    raise RunnerError(f"Readiness timeout for {urlparse_safe(url)} ({type(last).__name__})")


def urlparse_safe(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}{parts.path}"


def b64url_int(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def der(tag: int, payload: bytes) -> bytes:
    length = len(payload)
    if length < 128:
        length_bytes = bytes([length])
    else:
        raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
        length_bytes = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length_bytes + payload


def der_integer(value: bytes) -> bytes:
    value = value.lstrip(b"\x00") or b"\x00"
    if value[0] & 0x80:
        value = b"\x00" + value
    return der(0x02, value)


def jwk_to_pem(key: dict[str, str]) -> str:
    modulus = b64url_int(key["n"])
    exponent = b64url_int(key["e"])
    rsa_key = der(0x30, der_integer(modulus) + der_integer(exponent))
    rsa_algorithm = der(
        0x30,
        bytes.fromhex("06092a864886f70d010101") + bytes.fromhex("0500"),
    )
    subject_public_key_info = der(
        0x30,
        rsa_algorithm + der(0x03, b"\x00" + rsa_key),
    )
    encoded = base64.b64encode(subject_public_key_info).decode()
    wrapped = "\n".join(encoded[index : index + 64] for index in range(0, len(encoded), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----\n"


def generate_kong_config(keyset: dict[str, Any], output: Path) -> list[str]:
    keys = [
        key
        for key in keyset.get("keys", [])
        if key.get("kty") == "RSA"
        and key.get("use") == "sig"
        and key.get("alg") == "RS256"
        and key.get("kid")
        and key.get("n")
        and key.get("e")
    ]
    if not keys:
        raise RunnerError("Live Keycloak JWKS has no usable RS256 signing key")

    template = KONG_TEMPLATE.read_text()
    if "name: jwt" not in template or "claims_to_verify:\n        - exp" not in template:
        raise RunnerError("HTTP gateway template does not retain Kong expiry verification")
    if "pfp-authz" in template:
        raise RunnerError("The live integration must use bundled Kong plugins only")
    consumers = ["consumers:", "  - username: pfp-keycloak", "    jwt_secrets:"]
    for index, key in enumerate(keys):
        if index:
            consumers.append("      - key: " + json.dumps(key["kid"]))
        else:
            consumers.append("      - key: " + json.dumps(key["kid"]))
        consumers.extend(
            [
                "        algorithm: RS256",
                '        secret: "unused-for-rs256"',
                "        rsa_public_key: |",
            ]
        )
        consumers.extend("          " + line for line in jwk_to_pem(key).rstrip().splitlines())
    prefix, marker, _ = template.partition("consumers:")
    if not marker:
        raise RunnerError("Kong HTTP template has no consumer credential section")
    output.write_text(prefix.rstrip() + "\n" + "\n".join(consumers) + "\n")
    output.chmod(0o600)
    return [key["kid"] for key in keys]


def provision_identity(
    base_url: str,
    admin_token: str,
    run_id: str,
    user_password: str,
) -> tuple[str, str, str]:
    username = f"pfp-e2e-{run_id}"
    email = f"{username}@example.test"
    user = {
        "username": username,
        "enabled": True,
        "email": email,
        "emailVerified": True,
        "firstName": "PFP",
        "lastName": "Integration",
        "credentials": [
            {"type": "password", "value": user_password, "temporary": False}
        ],
    }
    status, headers, payload = admin_json(
        base_url,
        admin_token,
        "/admin/realms/pfp-e2e/users",
        "POST",
        user,
    )
    if status != 201:
        raise RunnerError(f"Keycloak synthetic user creation returned HTTP {status}")
    location = headers.get("Location", "")
    subject = location.rstrip("/").rsplit("/", 1)[-1]
    if not subject:
        matches_status, _, matches = admin_json(
            base_url,
            admin_token,
            f"/admin/realms/pfp-e2e/users?username={quote(username)}&exact=true",
        )
        if matches_status != 200 or not matches or len(matches) != 1:
            raise RunnerError("Unable to resolve the newly created Keycloak subject")
        subject = matches[0]["id"]
    if not re.fullmatch(r"[A-Za-z0-9-]{16,64}", subject):
        raise RunnerError("Keycloak returned an invalid synthetic subject identifier")
    return username, email, subject


def write_sanitized_log(path: Path, content: str) -> None:
    path.write_text(sanitize(content))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()

    if not shutil.which("docker"):
        raise RunnerError("docker is required")
    if not (FRONTEND / "node_modules" / ".bin" / "playwright").exists():
        raise RunnerError(
            "Frontend Playwright dependencies are missing; install from the lockfile first"
        )
    verify_tracked_inputs()

    run_id = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(6)
    project = f"pfp-live-{run_id}"
    db_name = f"pfp_e2e_auto_{run_id}"
    validate_database_name(db_name)

    ports = set()
    while len(ports) < 3:
        ports.add(allocate_port())
    web_port, kong_port, keycloak_port = sorted(ports)
    web_origin = f"http://127.0.0.1:{web_port}"
    keycloak_origin = f"http://127.0.0.1:{keycloak_port}"
    keycloak_hostname = f"http://localhost:{keycloak_port}"
    issuer = f"{keycloak_hostname}/realms/pfp-e2e"
    internal_jwks = (
        "http://keycloak:8080/realms/pfp-e2e/"
        "protocol/openid-connect/certs"
    )
    database_user = "pfp_e2e"
    database_password = secrets.token_urlsafe(36)
    database_url = make_database_url(database_user, database_password, db_name)
    ai_token = secrets.token_urlsafe(48)
    admin_password = secrets.token_urlsafe(36)
    user_password = secrets.token_urlsafe(36)
    api_secret = secrets.token_urlsafe(48)
    api_image = f"pfp-live-e2e-api:{run_id}"
    ai_image = f"pfp-live-e2e-ai:{run_id}"
    image_tags = [api_image, ai_image]

    artifacts_root = args.artifacts or Path("/tmp/pfp-live-e2e-artifacts")
    artifacts = artifacts_root / run_id
    artifacts.mkdir(parents=True, exist_ok=False)

    runtime_dir = Path(tempfile.mkdtemp(prefix="pfp-live-e2e-"))
    runtime_env = runtime_dir / "compose.env"
    runtime_realm = runtime_dir / "realm.json"
    kong_config = runtime_dir / "kong-http.yml"

    vite_process: subprocess.Popen[str] | None = None
    vite_log_handle = None
    vite_log_path = artifacts / "vite.log"
    compose: list[str] | None = None
    project_started = False

    try:
        kong_config.write_text(KONG_TEMPLATE.read_text())
        kong_config.chmod(0o644)
        write_runtime_realm(runtime_realm, web_origin)

        env_values = {
            "PFP_E2E_DATABASE_NAME": db_name,
            "PFP_E2E_DATABASE_USER": database_user,
            "PFP_E2E_DATABASE_PASSWORD": database_password,
            "PFP_E2E_DATABASE_URL": database_url,
            "PFP_E2E_OIDC_ISSUER": issuer,
            "PFP_E2E_OIDC_JWKS_URL": internal_jwks,
            "PFP_E2E_AI_INTERNAL_TOKEN": ai_token,
            "PFP_E2E_API_SECRET": api_secret,
            "PFP_E2E_KEYCLOAK_ADMIN_PASSWORD": admin_password,
            "PFP_E2E_KEYCLOAK_HOSTNAME": keycloak_hostname,
            "PFP_E2E_KEYCLOAK_PORT": str(keycloak_port),
            "PFP_E2E_KONG_PORT": str(kong_port),
            "PFP_E2E_REALM_FILE": str(runtime_realm),
            "PFP_E2E_API_IMAGE": api_image,
            "PFP_E2E_AI_IMAGE": ai_image,
            "PFP_E2E_KONG_CONFIG": str(kong_config),
            "PFP_E2E_SEED_SCRIPT": str(SEED_SCRIPT),
        }
        write_compose_env(runtime_env, env_values)
        compose = compose_args(project, runtime_env)

        assert_no_project_resources(project)
        config = json.loads(run(compose + ["config", "--format", "json"]).stdout)
        api_environment = config["services"]["api"]["environment"]
        validate_database_target(
            api_environment["DATABASE_URL"],
            database=db_name,
            user=database_user,
        )
        if api_environment.get("OIDC_ISSUER") != issuer:
            raise RunnerError("Effective FastAPI OIDC issuer does not match Keycloak")
        if config["services"]["postgres"].get("ports"):
            raise RunnerError("The isolated E2E PostgreSQL service must not publish a host port")
        if config["services"]["ai-service"].get("ports"):
            raise RunnerError("Go AI Service must not publish a host port")
        if any(network.get("external") for network in config.get("networks", {}).values()):
            raise RunnerError("The E2E project must not join an external Docker network")
        if any(volume.get("external") for volume in config.get("volumes", {}).values()):
            raise RunnerError("The E2E project must not use an external Docker volume")
        print(
            f"Isolated Compose project and database target verified: {project} / {db_name}"
        )

        run(
            compose + ["build", "--pull", "--no-cache", "api", "ai-service"],
            label="Building source-matched FastAPI and Go images",
        )

        project_started = True
        run(
            compose + ["up", "-d", "postgres", "keycloak"],
            label="Starting isolated PostgreSQL and Keycloak",
        )
        def postgres_identity() -> subprocess.CompletedProcess[str]:
            return postgres_exec(
                compose,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                database_user,
                "-d",
                db_name,
                "-Atqc",
                "SELECT current_database() || '|' || current_user",
            )

        wait_until(
            lambda: postgres_identity().returncode == 0,
            "isolated PostgreSQL database identity",
            120,
        )
        identity = postgres_identity()
        if identity.returncode:
            raise RunnerError("Connected PostgreSQL identity differs from the E2E target")
        validate_database_identity(
            identity.stdout.strip(),
            database=db_name,
            user=database_user,
        )
        table_count = postgres_exec(
            compose,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            database_user,
            "-d",
            db_name,
            "-Atqc",
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'",
        )
        if table_count.returncode or table_count.stdout.strip() != "0":
            raise RunnerError("The new E2E database was not empty before migration")
        project_volumes = compose_project_resources(project)["volumes"]
        if len(project_volumes) != 1:
            raise RunnerError("Expected exactly one disposable E2E PostgreSQL volume")
        print("E2E PostgreSQL is healthy, identity-checked, and empty before migration.")

        discovery_bytes = wait_http(
            f"{keycloak_origin}/realms/pfp-e2e/.well-known/openid-configuration"
        )
        discovery = json.loads(discovery_bytes)
        if discovery.get("issuer") != issuer:
            raise RunnerError("Live Keycloak issuer does not match the configured issuer")
        jwks_url = discovery.get("jwks_uri", "")
        if not jwks_url.startswith(keycloak_hostname + "/realms/pfp-e2e/"):
            raise RunnerError("Live Keycloak JWKS URI has an unexpected origin")
        with urlopen(jwks_url, timeout=8) as response:
            jwks = json.load(response)

        admin_token = identity_admin_token(keycloak_origin, admin_password)
        validate_live_client(keycloak_origin, admin_token, web_origin)
        kids = generate_kong_config(jwks, kong_config)
        username, email, subject = provision_identity(
            keycloak_origin,
            admin_token,
            run_id,
            user_password,
        )
        del admin_token
        print(
            f"Keycloak PKCE client and live RS256 JWKS verified "
            f"({len(kids)} signing key(s))."
        )
        print("Dedicated synthetic Keycloak identity provisioned with runtime credentials.")

        preflight_code = (
            "import os; from sqlalchemy import create_engine, text; "
            "from sqlalchemy.engine import make_url; "
            "u=make_url(os.environ['DATABASE_URL']); "
            f"assert u.database == {db_name!r}; "
            f"assert u.host == 'postgres' and u.username == {database_user!r}; "
            "e=create_engine(os.environ['DATABASE_URL']); "
            "c=e.connect(); actual,user=c.execute(text('SELECT current_database(), current_user')).one(); "
            f"assert actual == {db_name!r}; assert user == {database_user!r}; "
            "print('FastAPI/Alembic database identity verified:', actual, user); c.close(); e.dispose()"
        )
        run(
            compose + ["run", "--rm", "--no-deps", "api", "python", "-c", preflight_code],
            label="Verifying FastAPI/Alembic connection target",
        )
        run(
            compose + ["run", "--rm", "--no-deps", "api", "alembic", "upgrade", "head"],
            label="Migrating only the new E2E database",
        )
        current = run(
            compose + ["run", "--rm", "--no-deps", "api", "alembic", "current"],
            label="Verifying Alembic revision",
        ).stdout
        heads = run(
            compose + ["run", "--rm", "--no-deps", "api", "alembic", "heads"],
            label="Reading source migration head",
        ).stdout
        validate_alembic_revision(current, heads)
        print("Alembic migration completed on the isolated E2E database.")

        env_values.update(
            {
                "PFP_E2E_TEST_SUBJECT": subject,
                "PFP_E2E_TEST_EMAIL": email,
            }
        )
        write_compose_env(runtime_env, env_values)
        seed_output = run(
            compose
            + [
                "run",
                "--rm",
                "--no-deps",
                "api",
                "python",
                "/tmp/seed_live_owner.py",
            ],
            label="Seeding the synthetic owner mapping",
        ).stdout
        owner_id_match = re.search(r"PFP_E2E_OWNER_ID=(\d+)", seed_output)
        if not owner_id_match:
            raise RunnerError("Synthetic owner mapping did not return its owner identifier")
        owner_id = owner_id_match.group(1)

        run(
            compose + ["up", "-d", "--wait", "--wait-timeout", "120", "ai-service", "api"],
            label="Starting healthy Go and FastAPI services",
        )
        api_id = run(compose + ["ps", "-q", "api"]).stdout.strip()
        ai_id = run(compose + ["ps", "-q", "ai-service"]).stdout.strip()
        keycloak_id = run(compose + ["ps", "-q", "keycloak"]).stdout.strip()
        if not api_id or not ai_id or not keycloak_id:
            raise RunnerError("FastAPI, Go, or Keycloak container did not start")

        network_name = config["networks"]["default"]["name"]
        expected_services = {
            "ai-service": container_network_ip(ai_id, network_name),
            "keycloak": container_network_ip(keycloak_id, network_name),
        }
        for service_name, expected_ip in expected_services.items():
            resolved_ips = resolve_from_api(api_id, service_name)
            if resolved_ips != {expected_ip}:
                raise RunnerError(
                    f"Internal DNS for {service_name} does not resolve to its run container"
                )
        if not service_http_from_api(api_id, "http://ai-service:8091/health"):
            raise RunnerError("FastAPI cannot reach the Go health endpoint")
        if not service_http_from_api(api_id, internal_jwks):
            raise RunnerError("FastAPI cannot reach Keycloak JWKS")
        run(
            ["docker", "exec", api_id, "alembic", "current"],
            label="Confirming the running FastAPI Alembic target",
        )
        print("FastAPI readiness, isolated DNS, Go health, and Keycloak JWKS checks passed.")

        run(
            compose + ["up", "-d", "--wait", "--wait-timeout", "90", "kong"],
            label="Starting Kong after upstream readiness",
        )
        wait_http(f"http://127.0.0.1:{kong_port}/health/live", timeout=60)
        print("Kong is ready on loopback; PostgreSQL and Go are not host-published.")

        vite_env = os.environ.copy()
        vite_env.update(
            {
                "VITE_API_TARGET": f"http://127.0.0.1:{kong_port}",
                "VITE_KEYCLOAK_URL": keycloak_hostname,
                "VITE_KEYCLOAK_REALM": "pfp-e2e",
                "VITE_KEYCLOAK_CLIENT_ID": "moneta",
            }
        )
        vite_log_handle = vite_log_path.open("w")
        vite_process = subprocess.Popen(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                str(web_port),
                "--strictPort",
            ],
            cwd=FRONTEND,
            env=vite_env,
            stdout=vite_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_http(f"{web_origin}/", timeout=45)

        test_env = os.environ.copy()
        test_env.update(
            {
                "PFP_E2E_WEB_ORIGIN": web_origin,
                "PFP_E2E_ARTIFACT_DIR": str(artifacts / "playwright"),
                "PFP_E2E_KEYCLOAK_URL": keycloak_hostname,
                "PFP_E2E_USERNAME": username,
                "PFP_E2E_PASSWORD": user_password,
                "PFP_E2E_OWNER_ID": owner_id,
                "PFP_E2E_ACTIVE_KID": kids[0],
            }
        )
        playwright = FRONTEND / "node_modules" / ".bin" / "playwright"
        playwright_result = run(
            [
                str(playwright),
                "test",
                "--config",
                "playwright.live.config.ts",
                "e2e/live-financial-insights.spec.ts",
                "--workers=1",
            ],
            cwd=FRONTEND,
            env=test_env,
            check=False,
        )
        playwright_output = sanitize(playwright_result.stdout)
        (artifacts / "playwright.log").write_text(playwright_output)
        print(playwright_output[-8000:])
        if playwright_result.returncode:
            raise RunnerError("Live Playwright regression failed")

        print(
            f"Live browser E2E passed. Sanitized logs and failure artifacts: {artifacts}"
        )
        return 0
    except KeyboardInterrupt:
        print("Interrupted; cleaning only this run's services and volume.")
        return 130
    finally:
        cleanup_errors = []
        if vite_process is not None:
            try:
                if vite_process.poll() is None:
                    try:
                        os.killpg(vite_process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        vite_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(vite_process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        vite_process.wait(timeout=5)
                if vite_log_handle is not None:
                    vite_log_handle.close()
                if vite_log_path.exists():
                    write_sanitized_log(
                        vite_log_path, vite_log_path.read_text(errors="replace")
                    )
            except Exception as error:
                cleanup_errors.append(f"Vite cleanup failed: {error}")
        elif vite_log_handle is not None:
            try:
                vite_log_handle.close()
            except OSError as error:
                cleanup_errors.append(f"Vite log close failed: {error}")

        if compose is not None and project_started:
            try:
                collect_service_logs(compose, artifacts)
            except Exception as error:
                cleanup_errors.append(f"service log collection failed: {error}")
            try:
                teardown = run(
                    compose + ["down", "--volumes", "--remove-orphans"],
                    check=False,
                )
                if teardown.returncode:
                    cleanup_errors.append("project-scoped Compose teardown failed")
            except Exception as error:
                cleanup_errors.append(
                    f"project-scoped Compose teardown failed: {error}"
                )
            try:
                assert_no_project_resources(project)
            except RunnerError as error:
                cleanup_errors.append(str(error))

        for image in image_tags:
            try:
                present = run(["docker", "image", "inspect", image], check=False)
                if present.returncode == 0:
                    removed = run(["docker", "image", "rm", image], check=False)
                    if removed.returncode:
                        cleanup_errors.append(f"could not remove run image {image}")
            except Exception as error:
                cleanup_errors.append(f"image cleanup failed for {image}: {error}")

        try:
            shutil.rmtree(runtime_dir)
        except FileNotFoundError:
            pass
        except OSError as error:
            cleanup_errors.append(f"could not remove runtime secret directory: {error}")

        if cleanup_errors:
            for error in cleanup_errors:
                print(f"E2E cleanup error: {sanitize(error)}", file=sys.stderr)
            if sys.exc_info()[0] is None:
                raise RunnerError("E2E cleanup did not complete successfully")


def wait_until(predicate, label: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RunnerError(f"Timed out waiting for {label}")


def service_http_from_api(container_id: str, url: str) -> bool:
    code = (
        "import urllib.request; r=urllib.request.urlopen(" + repr(url) + ",timeout=3); "
        "assert r.status == 200; r.read(4096)"
    )
    return run(
        ["docker", "exec", container_id, "python", "-c", code],
        check=False,
    ).returncode == 0


def container_network_ip(container_id: str, network_name: str) -> str:
    networks = json.loads(
        run(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id]
        ).stdout
    )
    address = networks.get(network_name, {}).get("IPAddress", "")
    if not address:
        raise RunnerError("A required service is not attached to the E2E network")
    return address


def resolve_from_api(api_id: str, hostname: str) -> set[str]:
    code = (
        "import json,socket; print(json.dumps(sorted({row[4][0] for row in "
        "socket.getaddrinfo(" + repr(hostname) + ",None)})))"
    )
    result = run(
        ["docker", "exec", api_id, "python", "-c", code],
        label=f"Resolving internal service alias {hostname}",
    )
    try:
        return set(json.loads(result.stdout.strip().splitlines()[-1]))
    except (IndexError, json.JSONDecodeError, TypeError) as error:
        raise RunnerError("Internal Docker DNS returned an invalid response") from error


def collect_service_logs(compose: list[str], artifacts: Path) -> None:
    log_dir = artifacts / "services"
    log_dir.mkdir(parents=True, exist_ok=True)
    for service in ("postgres", "keycloak", "api", "ai-service", "kong"):
        result = run(
            compose + ["logs", "--no-color", "--tail=500", service],
            check=False,
        )
        (log_dir / f"{service}.log").write_text(sanitize(result.stdout))


def install_signal_handlers() -> None:
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)


if __name__ == "__main__":
    install_signal_handlers()
    try:
        sys.exit(main())
    except RunnerError as error:
        print(f"E2E preflight/run failed: {sanitize(str(error))}", file=sys.stderr)
        sys.exit(1)
