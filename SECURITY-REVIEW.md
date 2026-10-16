# Authentication and Authorization Review

## Scope and conclusion

This read-only review examined the JWT and Keycloak OIDC paths, authorization policy model, Kong's authorization integration, ownership enforcement in the activity-stream path, and the cited authorization tests. No exploitable authorization bypass was confirmed in the reviewed evidence.

The principal gap is a capability mismatch for an API Platform backend: the evidenced policy model does not represent role/permission grants or general resource-attribute rules. This is not evidence of a current privilege-escalation exploit. The cited tests were inspected, not rerun.

The endpoint-specific evidence captured here covers the activity-stream routes and the `/accounts` Kong pilot. It does not constitute a verified inventory of every API endpoint.

## Existing architecture

### Authentication

- Local JWT claims are verified in [`backend/app/auth.py`](backend/app/auth.py#L45).
- Keycloak OIDC tokens are signature- and claim-validated in [`backend/app/oidc.py`](backend/app/oidc.py#L74), then mapped to a local principal in [`backend/app/routers/auth.py`](backend/app/routers/auth.py#L134).
- The mapped principal carries identity, not Keycloak roles or permissions ([`backend/app/routers/auth.py`](backend/app/routers/auth.py#L109)).

### Authorization and ownership

- The policy model has public, metrics-token, and owner access with fixed resource/action enums. `Subject` contains a user ID and `Resource` an owner ID ([`backend/app/authorization.py`](backend/app/authorization.py#L18)).
- Activity-stream routes use owner-principal guards. The handlers derive the user from the authenticated principal; event and cursor lookups are scoped by user ([`backend/app/activity_feed_reader.py`](backend/app/activity_feed_reader.py#L38), [`backend/app/activity_feed_reader.py`](backend/app/activity_feed_reader.py#L96)).
- These principal-to-owner checks and owner-scoped SQL filters provide implicit ownership-based ABAC for covered resources.

### Gateway

- Kong attaches `pfp-authz` to the `/accounts` route ([`gateway/kong/kong-compose.yml`](gateway/kong/kong-compose.yml#L8)).
- The corresponding backend gateway authorizer is scoped to the accounts pilot ([`backend/app/routers/gateway_authz.py`](backend/app/routers/gateway_authz.py#L31)). Other API routes rely on backend authentication and authorization controls; the reviewed gateway integration is not a general cross-service policy engine.
- A route not covered by the Kong authorizer is not, by itself, an authorization bypass if backend controls remain enforced. Backend authorization should remain authoritative rather than trusting caller-supplied identity headers.

## RBAC coverage

No role/permission grant mechanism was evidenced in the cited policy and principal paths. The fixed policy types do not represent role membership, permissions, or role-based privilege levels. Role-based authorization and privilege escalation behavior are therefore not covered by the reviewed policy tests.

## ABAC coverage

- **Implemented:** Ownership is an implicit resource attribute. The activity-stream SQL reader scopes event and cursor retrieval by user.
- **Not evidenced:** General resource or contextual attributes such as tenant, account state, classification, or action-specific business attributes in policy decisions.
- **Cross-service:** The Kong authorization integration is limited to the accounts pilot. Consistent policy evaluation across services is not evidenced.

## Findings

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | ⚪ LOW | [`backend/app/authorization.py`](backend/app/authorization.py) | 18-60 | The policy model supports fixed public/metrics-token/owner rules, but does not express role/permission grants or general resource-attribute rules. Existing ownership checks mitigate cross-user access for covered owner-scoped resources; future endpoints requiring these controls cannot express them in this policy model. This is a requirements/capability gap, not a demonstrated exploit. | 9/10 |

No specific endpoint that bypasses authorization or returns another user's data was identified in the reviewed paths. The evidence in this review is not a complete endpoint-by-endpoint verification.

## Authorization tests reviewed

- [`backend/tests/test_activity_stream.py`](backend/tests/test_activity_stream.py#L48): stream authentication and cursor rejection.
- [`backend/tests/test_activity_shared_stream.py`](backend/tests/test_activity_shared_stream.py#L150): user isolation.
- [`backend/tests/test_authorization_policy.py`](backend/tests/test_authorization_policy.py#L464): cross-user reads/writes, nested-resource ownership, and denial without side effects.
- [`backend/tests/test_authorization_policy.py`](backend/tests/test_authorization_policy.py#L706): a valid JWT alone does not grant access to another user's objects.
- Not evidenced in these test paths: role/permission escalation tests and attribute-based policy allow/deny tests.

## Minimal recommended implementation plan

1. Map verified Keycloak role/permission claims—or a trusted server-side role mapping—into the principal. Do not derive authorization from untrusted request headers.
2. Add explicit permission and resource-attribute predicates to route policies only where required; retain owner-scoped SQL filters and object checks.
3. Add focused allow/deny tests for role escalation and attribute-based access, retaining the existing cross-user and unauthorized-write coverage.
4. If Kong is intended to enforce policy across services, generalize the accounts-only integration and attach it to all in-scope routes. Otherwise, document it as defense in depth rather than the canonical policy engine.
