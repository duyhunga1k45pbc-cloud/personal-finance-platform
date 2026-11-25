import base64
import json
import subprocess
from pathlib import Path

import pytest

import run_live_e2e as runner


def completed(args, returncode=0, stdout=""):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


@pytest.mark.parametrize(
    "database",
    ["finance_db", "finance_test", "pfp_smoke_test", "not-a-test-database"],
)
def test_database_name_guard_rejects_protected_or_unscoped_names(database):
    with pytest.raises(runner.RunnerError, match="database name"):
        runner.validate_database_name(database)


def test_database_target_and_identity_must_match_isolated_test_database():
    runner.validate_database_name("pfp_e2e_auto_20261126000000abc123")
    target = "postgresql://pfp_e2e:secret@postgres:5432/pfp_e2e_auto_20261126000000abc123"
    runner.validate_database_target(
        target,
        database="pfp_e2e_auto_20261126000000abc123",
        user="pfp_e2e",
    )
    runner.validate_database_identity(
        "pfp_e2e_auto_20261126000000abc123|pfp_e2e",
        database="pfp_e2e_auto_20261126000000abc123",
        user="pfp_e2e",
    )

    with pytest.raises(runner.RunnerError, match="generated test database"):
        runner.validate_database_target(
            "postgresql://pfp_e2e:secret@postgres:5432/finance_db",
            database="pfp_e2e_auto_20261126000000abc123",
            user="pfp_e2e",
        )
    with pytest.raises(runner.RunnerError, match="identity differs"):
        runner.validate_database_identity(
            "finance_db|postgres",
            database="pfp_e2e_auto_20261126000000abc123",
            user="pfp_e2e",
        )


def test_alembic_revision_validation_requires_every_source_head():
    runner.validate_alembic_revision(
        "abc123 (head)\n", "abc123 (head)\n"
    )
    runner.validate_alembic_revision(
        "abc123 (head)\ndef456 (head)\n",
        "abc123 (head)\ndef456 (head)\n",
    )
    for current, heads in (
        ("abc123 (head)", "def456 (head)\n"),
        ("abc123 (head)", ""),
        ("abc123 (head)\ndef456 (head)\n", "abc123 (head)\nghi789 (head)\n"),
        ("abc-def (head)", "abc (head)\n"),
    ):
        with pytest.raises(runner.RunnerError, match="Alembic head"):
            runner.validate_alembic_revision(current, heads)


def test_docker_resource_inspection_is_scoped_to_compose_project(monkeypatch):
    calls = []

    def fake_run(args, *, check=False):
        calls.append(args)
        resource = args[1]
        result = {
            "container": "container-id\n",
            "network": "network-id\n",
            "volume": "volume-id\n",
        }[resource]
        return completed(args, stdout=result)

    monkeypatch.setattr(runner, "run", fake_run)
    resources = runner.compose_project_resources("pfp-live-test")

    assert resources == {
        "containers": ["container-id"],
        "networks": ["network-id"],
        "volumes": ["volume-id"],
    }
    assert len(calls) == 3
    assert all(
        args[-1] == "label=com.docker.compose.project=pfp-live-test"
        for args in calls
    )
    monkeypatch.setattr(
        runner,
        "compose_project_resources",
        lambda _project: {
            "containers": ["other-project-container"],
            "networks": [],
            "volumes": [],
        },
    )
    with pytest.raises(runner.RunnerError, match="Run-scoped Docker resources"):
        runner.assert_no_project_resources("pfp-live-test")


def test_docker_command_failure_propagates_with_sanitized_output(monkeypatch, capsys):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda args, **_kwargs: completed(
            args, returncode=17, stdout="password=unit-secret"
        ),
    )

    with pytest.raises(runner.RunnerError, match="compose startup"):
        runner.run(["docker", "compose", "up"], label="compose startup")
    output = capsys.readouterr().out
    assert "unit-secret" not in output
    assert "[REDACTED]" in output


def test_runtime_realm_is_rewritten_for_pkce_and_drops_static_users(
    monkeypatch, tmp_path
):
    source = tmp_path / "realm-source.json"
    source.write_text(
        json.dumps(
            {
                "users": [{"username": "fixture-user"}],
                "clients": [
                    {
                        "clientId": "moneta",
                        "attributes": {},
                        "redirectUris": ["old-origin/*"],
                        "webOrigins": ["old-origin"],
                    }
                ],
            }
        )
    )
    output = tmp_path / "realm-runtime.json"
    monkeypatch.setattr(runner, "REALM_FILE", source)

    runner.write_runtime_realm(output, "http://127.0.0.1:41234")

    realm = json.loads(output.read_text())
    assert realm["users"] == []
    client = realm["clients"][0]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == ["http://127.0.0.1:41234/*"]
    assert client["webOrigins"] == ["http://127.0.0.1:41234"]
    assert output.stat().st_mode & 0o777 == 0o644


def test_kong_config_accepts_only_rs256_signing_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "KONG_TEMPLATE",
        tmp_path / "kong-template.yml",
    )
    runner.KONG_TEMPLATE.write_text(
        "plugins:\n"
        "  - name: jwt\n"
        "    config:\n"
        "      claims_to_verify:\n"
        "        - exp\n"
        "consumers:\n"
    )
    output = tmp_path / "kong-runtime.yml"
    modulus = base64.urlsafe_b64encode(b"\x01" + b"\x00" * 127).rstrip(b"=").decode()
    exponent = base64.urlsafe_b64encode(b"\x01\x00\x01").rstrip(b"=").decode()
    keys = [
        {"kty": "EC", "use": "sig", "alg": "RS256", "kid": "not-rsa"},
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "active-key",
            "n": modulus,
            "e": exponent,
        },
    ]

    assert runner.generate_kong_config({"keys": keys}, output) == ["active-key"]
    rendered = output.read_text()
    assert 'key: "active-key"' in rendered
    assert "algorithm: RS256" in rendered
    assert "BEGIN PUBLIC KEY" in rendered
    assert output.stat().st_mode & 0o777 == 0o600

    with pytest.raises(runner.RunnerError, match="no usable RS256"):
        runner.generate_kong_config({"keys": []}, output)


def test_live_keycloak_client_validation_requires_pkce_and_audience(monkeypatch):
    client = {
        "publicClient": True,
        "standardFlowEnabled": True,
        "attributes": {"pkce.code.challenge.method": "S256"},
        "redirectUris": ["http://127.0.0.1:41234/*"],
        "webOrigins": ["http://127.0.0.1:41234"],
        "protocolMappers": [
            {
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": "pfp-api",
                    "access.token.claim": "true",
                },
            }
        ],
    }
    monkeypatch.setattr(
        runner,
        "admin_json",
        lambda *_args, **_kwargs: (200, {}, [client]),
    )
    runner.validate_live_client(
        "http://localhost:41234",
        "token",
        "http://127.0.0.1:41234",
    )
    client["attributes"]["pkce.code.challenge.method"] = "plain"
    with pytest.raises(runner.RunnerError, match="PKCE S256"):
        runner.validate_live_client(
            "http://localhost:41234",
            "token",
            "http://127.0.0.1:41234",
        )


def test_http_readiness_timeout_does_not_expose_url_credentials(monkeypatch):
    ticks = iter([0.0, 0.5, 1.1])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    def unavailable(*_args, **_kwargs):
        raise OSError("not ready")

    monkeypatch.setattr(runner, "urlopen", unavailable)
    with pytest.raises(runner.RunnerError, match="Readiness timeout") as error:
        runner.wait_http("http://user:secret@localhost:8123/health?token=hidden", timeout=1)
    assert "secret" not in str(error.value)
    assert "hidden" not in str(error.value)


def prepare_mocked_main(monkeypatch, tmp_path, *, failure, cleanup_failure=False):
    frontend = tmp_path / "frontend"
    playwright = frontend / "node_modules" / ".bin" / "playwright"
    playwright.parent.mkdir(parents=True)
    playwright.write_text("")
    monkeypatch.setattr(runner, "FRONTEND", frontend)
    monkeypatch.setattr(runner, "verify_tracked_inputs", lambda: None)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner.time, "strftime", lambda *_args: "20261126000000")
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _size: "a1b2c3")
    monkeypatch.setattr(runner.secrets, "token_urlsafe", lambda _size: "unit-secret")
    ports = iter([41001, 41002, 41003])
    monkeypatch.setattr(runner, "allocate_port", lambda: next(ports))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setattr(
        runner.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(runtime_dir),
    )
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(
        runner.sys,
        "argv",
        ["run_live_e2e.py", "--artifacts", str(artifacts)],
    )
    monkeypatch.setattr(
        runner,
        "compose_args",
        lambda project, env_file: ["compose-marker", str(env_file)],
    )
    monkeypatch.setattr(runner, "write_runtime_realm", lambda *_args: None)
    monkeypatch.setattr(runner, "identity_admin_token", lambda *_args: "admin-token")
    monkeypatch.setattr(runner, "validate_live_client", lambda *_args: None)
    monkeypatch.setattr(runner, "generate_kong_config", lambda *_args: ["active-kid"])
    monkeypatch.setattr(
        runner,
        "provision_identity",
        lambda *_args: ("e2e-user", "e2e@example.test", "subject-1234567890123456"),
    )
    monkeypatch.setattr(
        runner,
        "service_http_from_api",
        lambda *_args: True,
    )
    monkeypatch.setattr(runner, "container_network_ip", lambda cid, _net: {
        "ai-service-container": "10.0.0.2",
        "keycloak-container": "10.0.0.3",
    }[cid])
    monkeypatch.setattr(
        runner,
        "resolve_from_api",
        lambda _api, hostname: {
            "ai-service": {"10.0.0.2"},
            "keycloak": {"10.0.0.3"},
        }[hostname],
    )
    monkeypatch.setattr(
        runner,
        "wait_http",
        lambda url, **_kwargs: (
            json.dumps(
                {
                    "issuer": "http://localhost:41003/realms/pfp-e2e",
                    "jwks_uri": "http://localhost:41003/realms/pfp-e2e/certs",
                }
            ).encode()
            if ".well-known" in url
            else b"ok"
        ),
    )
    class MockJWKSResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"keys":[{"kid":"active-kid"}]}'

    monkeypatch.setattr(
        runner,
        "urlopen",
        lambda *_args, **_kwargs: MockJWKSResponse(),
    )

    def fake_postgres_exec(_compose, *args):
        joined = " ".join(args)
        if "current_database()" in joined:
            database = args[args.index("-d") + 1]
            return completed(args, stdout=f"{database}|pfp_e2e\n")
        if "count(*)" in joined:
            return completed(args, stdout="0\n")
        raise AssertionError(f"Unexpected mocked PostgreSQL command: {args}")

    monkeypatch.setattr(runner, "postgres_exec", fake_postgres_exec)
    monkeypatch.setattr(
        runner,
        "compose_project_resources",
        lambda _project: {
            "containers": [],
            "networks": [],
            "volumes": ["disposable-volume"],
        },
    )
    resource_checks = []
    monkeypatch.setattr(
        runner,
        "assert_no_project_resources",
        lambda project: resource_checks.append(project),
    )
    logs_collected = []
    monkeypatch.setattr(
        runner,
        "collect_service_logs",
        lambda _compose, artifacts_dir: logs_collected.append(artifacts_dir),
    )
    popen_calls = []

    class FinishedVite:
        pid = 12345

        def poll(self):
            return 0

    def fake_popen(*_args, **_kwargs):
        popen_calls.append(True)
        return FinishedVite()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    compose_calls = []

    def fake_run(args, *, check=True, label=None, **_kwargs):
        if args[0] == "compose-marker":
            suffix = args[2:]
            compose_calls.append(suffix)
            if suffix == ["config", "--format", "json"]:
                env_path = Path(args[1])
                values = {
                    key: value
                    for line in env_path.read_text().splitlines()
                    for key, separator, value in [line.partition("=")]
                    if separator
                }
                values["DATABASE_URL"] = values["PFP_E2E_DATABASE_URL"]
                values["OIDC_ISSUER"] = values["PFP_E2E_OIDC_ISSUER"]
                config = {
                    "services": {
                        "api": {"environment": values},
                        "postgres": {"ports": None},
                        "ai-service": {"ports": None},
                    },
                    "networks": {"default": {"name": "fake-network", "external": False}},
                    "volumes": {"data": {"external": False}},
                }
                return completed(args, stdout=json.dumps(config))
            if suffix[:1] == ["down"]:
                return completed(
                    args,
                    returncode=1 if cleanup_failure else 0,
                    stdout="teardown failed" if cleanup_failure else "",
                )
            if failure == "setup" and suffix[:1] == ["up"]:
                raise runner.RunnerError("Injected Compose startup failure")
            if suffix[:2] == ["ps", "-q"]:
                return completed(args, stdout=f"{suffix[-1]}-container\n")
            if suffix[:2] == ["run", "--rm"]:
                if "heads" in suffix:
                    return completed(args, stdout="rev1 (head)\n")
                if "current" in suffix:
                    return completed(args, stdout="rev1 (head)\n")
                if "upgrade" in suffix:
                    return completed(args)
                if any("seed_live_owner.py" in arg for arg in suffix):
                    return completed(args, stdout="PFP_E2E_OWNER_ID=42\n")
                if "python" in suffix:
                    return completed(args)
            if suffix[:1] == ["build"]:
                return completed(args)
            if suffix[:1] == ["up"]:
                return completed(args)
        elif args[:2] == ["docker", "exec"]:
            return completed(args, stdout="rev1 (head)\n")
        elif args[:3] == ["docker", "image", "inspect"]:
            return completed(args, returncode=1)
        elif str(args[0]).endswith("/playwright"):
            return completed(
                args,
                returncode=1 if failure == "playwright" else 0,
                stdout="Browser error: Bearer unit-secret",
            )
        if check:
            raise AssertionError(f"Unexpected mocked command: {args}; label={label}")
        return completed(args, returncode=1)

    monkeypatch.setattr(runner, "run", fake_run)
    return {
        "artifacts": artifacts,
        "compose_calls": compose_calls,
        "logs_collected": logs_collected,
        "popen_calls": popen_calls,
        "resource_checks": resource_checks,
        "runtime_dir": runtime_dir,
    }


@pytest.mark.parametrize(
    ("failure", "cleanup_failure", "expected_error"),
    [
        ("setup", False, "Injected Compose startup failure"),
        ("playwright", False, "Live Playwright regression failed"),
        ("setup", True, "Injected Compose startup failure"),
    ],
)
def test_main_propagates_failures_and_cleans_only_run_resources(
    monkeypatch,
    tmp_path,
    capsys,
    failure,
    cleanup_failure,
    expected_error,
):
    state = prepare_mocked_main(
        monkeypatch,
        tmp_path,
        failure=failure,
        cleanup_failure=cleanup_failure,
    )

    with pytest.raises(runner.RunnerError, match=expected_error):
        runner.main()

    assert state["runtime_dir"].exists() is False
    assert state["logs_collected"]
    assert state["resource_checks"]
    assert any(call[:1] == ["down"] for call in state["compose_calls"])
    if failure == "playwright":
        playwright_log = next(state["artifacts"].rglob("playwright.log"))
        sanitized = playwright_log.read_text()
        assert "unit-secret" not in sanitized
        assert "[REDACTED]" in sanitized
    if cleanup_failure:
        assert "project-scoped Compose teardown failed" in capsys.readouterr().err
