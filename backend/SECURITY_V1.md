# V1 Security Review

TASK 15 hardens the existing V1 boundary without changing financial-domain
semantics or adding infrastructure without a failure-mode justification.

## Security invariants now enforced

1. **Production secrets fail closed.** `APP_ENV=production` refuses to start
   without an explicit `SECRET_KEY` of at least 32 characters. Known placeholder
   values are rejected.
2. **JWT verification is strict.** Access tokens carry and verify issuer,
   audience, issued-at, not-before, expiry, type, subject, and unique token ID.
   V1 allows HS256 only.
3. **Principal identity is stable.** JWT `sub` is the immutable database user ID,
   not an email address.
4. **Authentication failures are uniform.** Missing, malformed, expired,
   wrong-audience, wrong-issuer, wrong-type, and deleted-principal tokens all
   return `401` with `WWW-Authenticate: Bearer`.
5. **Login does not skip password verification for unknown accounts.** A dummy
   bcrypt verification reduces the account-enumeration timing difference.
6. **Email identity is normalized.** Registration and login compare lower-case
   normalized addresses; concurrent duplicate registration is contained as a
   `409` with rollback.
7. **Request models reject unexpected auth fields.** Passwords remain bounded by
   bcrypt's 72-byte compatibility limit and registration requires at least 8
   characters.
8. **No repository database credential fallback.** `DATABASE_URL` must come from
   environment or a local `.env` file.
9. **Sensitive API responses are non-cacheable.** API responses include
   `Cache-Control: no-store`, `Pragma: no-cache`, `X-Content-Type-Options:
   nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.
10. **Production API discovery is closed by default.** FastAPI Swagger/ReDoc and
    OpenAPI routes are disabled when `APP_ENV=production`.
11. **Authentication coverage is regression-tested.** Every non-public V1 API
    operation must declare Bearer authentication in OpenAPI, except the explicit
    allowlist: `/`, `/auth/register`, `/auth/login`.
12. **Ownership remains a domain invariant.** Existing account/provider/
    reconciliation/projection tests and the V1 acceptance suite continue to
    prove cross-user isolation.

## Deliberate remaining production gates

These are intentionally not faked with weak local mechanisms:

- **Rate limiting:** not implemented in-process because that would be incorrect
  across multiple workers/instances. Before exposing the service publicly,
  enforce login/register and API limits at a shared edge/gateway or shared
  durable limiter.
- **Dependency vulnerability scanning:** add a locked dependency manifest and
  run a scanner such as `pip-audit` in CI. The current repository snapshot has
  no committed requirements/lock file, so claiming a reproducible dependency
  audit would be false.
- **TLS/HSTS:** terminate HTTPS at the deployment boundary. Enable HSTS there
  only after the real HTTPS topology is known.
- **Token revocation/refresh rotation:** V1 uses short-lived access tokens only.
  Add refresh/revocation state only when the product needs long-lived sessions.

The rule remains: add a mechanism only when a concrete failure mode requires it.
