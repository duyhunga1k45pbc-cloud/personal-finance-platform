# Kong gateway scope and operations

## Backend reachability

The gateway deployment in `compose.internal.yml` does not publish its FastAPI
container to the host: the API service has `expose: 8000` for Docker-network
communication and no host `ports` mapping. Kong is published only on
`127.0.0.1:8004`. The standalone `backend/docker-compose.yml` separately
publishes port `8000` for local backend development; keep that workflow
unchanged. If that standalone stack is running, its host-published API remains
reachable independently of Kong and therefore does not inherit Kong policies.
Do not treat it as part of the gateway-only deployment.

The full-stack E2E Compose file follows the same host-boundary rule: API has no
host port, while Kong is bound to `127.0.0.1:18004`. Its PostgreSQL and
Keycloak ports are also loopback-only.

## Login routes through Kong

The legacy FastAPI `POST /auth/login` and `POST /auth/register` routes are
intentionally unavailable through Kong. The Kong JWT plugin is scoped to the
`pfp-api` service, so those routes require a Keycloak-issued bearer token
before they can reach FastAPI. They remain available directly in the
standalone backend development workflow. Gateway clients authenticate with
Keycloak; no public password-login or registration flow is part of this
gateway scope.

## JWT signing-key rotation

Kong's declarative `jwt_secrets` configuration contains a fixed signing-key
ID (`kid`) and RSA public key in `kong/kong-compose.yml`. Kong does not fetch
Keycloak JWKS dynamically. A newly active Keycloak signing key will therefore
be rejected by Kong until its `kid` and public key are configured. The FastAPI
OIDC verifier refreshes JWKS for an unknown key ID, but that does not refresh
Kong's independent JWT verifier.

Rotate manually with an overlap period:

1. In Keycloak, create the next RS256 signing key but keep the current signing
   key active. Obtain only the new key's `kid` and public key from the realm
   JWKS; never copy a private key into this repository.
2. Add a second `jwt_secrets` entry under the `pfp-keycloak` consumer in
   `kong/kong-compose.yml`, using the new `kid`, `algorithm: RS256`, and its
   public key. Retain the old entry.
3. Recreate Kong to load the declarative configuration:
   `docker compose -f gateway/compose.internal.yml up -d --force-recreate kong`
4. Activate the new signing key in Keycloak. Verify that a freshly issued
   token succeeds through Kong and that an old-key token still works during
   the overlap period.
5. Keep both entries until old tokens have expired (including the configured
   token lifetime and clock-skew allowance). Then remove the old entry and
   recreate Kong using the same command.

If the new key is activated before Kong is updated, requests using tokens
signed by that key fail closed at Kong. This procedure is manual; rotation is
not automatic or zero-downtime.

## E2E configuration parity

`e2e/bootstrap.py` starts from `kong/kong-compose.yml` and changes only the
API service address and the test realm's dynamic JWT key ID/public key. The
E2E regression test normalizes those environment-specific values and asserts
that the rest of the generated Kong configuration matches the gateway
deployment, including route/plugin scope and JWT verification settings.

## Optional gateway lab modes

Two gateway configurations are available. They intentionally share the same
FastAPI financial backend; neither configuration is required for the standalone
backend workflow.

| Mode | Compose file | Purpose |
| --- | --- | --- |
| External AuthZ (existing default) | `gateway/compose.internal.yml` | Kong JWT + route-specific `pfp-authz` (fail closed), plus HTTP gateway plugins |
| HTTP infrastructure lab | `gateway/compose.http.yml` | Kong routing, request IDs, IP rate limiting and JWT verification; **no** `pfp-authz` call |

The HTTP lab *retains Kong JWT verification* so a token-free request is not
silently allowed through a protected route. FastAPI independently authenticates
and authorizes requests in **both** modes, including resource ownership checks.
The absence of `pfp-authz` in HTTP mode changes the **gateway-level** policy:
for example, HTTP mode does not apply the pilot's special `POST /accounts`
DENY rule. FastAPI then makes its own decision. Do not describe the two modes
as having identical access policies.

From the repository root, with the existing `backend_default` Docker network,
API container environment file, and the appropriate Keycloak setup available:

```sh
# Existing External AuthZ mode; requires PFP_GATEWAY_AUTHZ_SECRET.
docker compose -f gateway/compose.internal.yml up -d --build

# Stop it before changing modes (both use 127.0.0.1:8004).
docker compose -f gateway/compose.internal.yml down

# HTTP lab: no custom pfp-authz image or shared secret is needed.
docker compose -f gateway/compose.http.yml up -d --build

# Stop HTTP lab before returning to External AuthZ mode.
docker compose -f gateway/compose.http.yml down
```

Both lab files expose only Kong to the loopback host. Their API container is
reachable via the Docker network, not via an additional host port. The separate
standalone backend Docker Compose remains unchanged, and its independently
published host port bypasses Kong policies.

The HTTP lab is *not* an anonymous API: the bundled JWT plugin remains enabled
on the `pfp-api` service, with the same static key rotation limitations
described above. To study gateway behavior with a different JWT setup, use a
separate explicit experiment rather than silently relaxing this mode.

### Verify the boundary before making changes

Run `python -m pytest -q gateway/tests/test_lab_mode_configs.py` to check that
both Kong declarative configurations maintain the same routes/JWT/rate-limit
settings, while only the External AuthZ mode attaches `pfp-authz`.
These static checks do **not** replace the existing real-stack security E2E
suite. Continue running that suite against the External AuthZ mode before
accepting security-sensitive changes.
