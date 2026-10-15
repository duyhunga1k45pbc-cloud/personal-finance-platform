"""Static regression tests for the optional Kong lab mode split.

No Docker dependency: verify that only the intended plugin/enforcement differs.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway"


def test_http_gateway_is_authz_config_minus_custom_route_plugin():
    authz = (GATEWAY / "kong/kong-compose.yml").read_text()
    http = (GATEWAY / "kong/kong-http.yml").read_text()
    custom_route = '''      - name: pfp-accounts-authz
        paths:
          - /accounts
        strip_path: false
        plugins:
          - name: pfp-authz
            config:
              authz_url: http://pfp-api-compose:8000/internal/gateway/authorize
              shared_secret: "{vault://env/gateway-authz-secret}"
              timeout_ms: 1000
'''
    assert authz.count(custom_route) == 1
    assert http == authz.replace(custom_route, "")
    assert "pfp-authz" not in http
    for name in ("jwt", "correlation-id", "rate-limiting"):
        assert f"name: {name}" in http


def test_compose_modes_preserve_boundary_and_do_not_share_authz_secret():
    authz = (GATEWAY / "compose.internal.yml").read_text()
    http = (GATEWAY / "compose.http.yml").read_text()
    assert '"127.0.0.1:8004:8000"' in authz
    assert '"127.0.0.1:8004:8000"' in http
    for config in (authz, http):
        assert 'expose:\n      - "8000"' in config
        assert 'KONG_DATABASE: "off"' in config
        assert "backend_default:" in config
    assert "pfp-authz" in authz
    assert "PFP_GATEWAY_AUTHZ_SECRET" in authz
    assert "pfp-authz" not in http
    assert "PFP_GATEWAY_AUTHZ_SECRET" not in http
    assert 'KONG_PLUGINS: "bundled"' in http
    assert "./kong/kong-http.yml:" in http
