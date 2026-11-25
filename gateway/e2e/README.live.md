# Live Financial Insights browser E2E

This runs the real React → Keycloak PKCE S256 → Kong → FastAPI → PostgreSQL →
Go deterministic-provider flow using the existing live Playwright test. No API
or Keycloak routes are mocked.

## Prerequisites

- Python 3.10 or newer.
- Docker Engine with the Compose plugin and permission to build/pull images.
- Node.js/npm, frontend dependencies, and the Playwright Chromium browser.

For a clean checkout, install the locked frontend dependencies and browser once:

```bash
cd frontend
npm ci
npx playwright install chromium
cd ..
```

No existing PFP containers, Docker networks, database, volumes, local `.env`
files, or manually configured Keycloak instance are required. The runner
generates all test credentials at runtime.

## Run

From the repository root, execute:

```bash
python3 gateway/e2e/run_live_e2e.py
```

The runner allocates free loopback ports for Vite, Kong, and Keycloak, and
generates a unique Compose project and `pfp_e2e_auto_*` database name for each
invocation. It refuses to proceed unless the required source and configuration
paths are tracked by Git.

## Isolation and cleanup

Each run provisions its own PostgreSQL container and project-scoped named data
volume, Keycloak container, FastAPI container, Go service, Kong container, and
Compose network. PostgreSQL is not published to the host. The runner verifies
the connected database name and role, and confirms the database has no public
tables before running Alembic. Migrations and synthetic owner seeding use the
same explicit database URL; account and transaction data are created through
the real UI and FastAPI endpoints.

The runner verifies Keycloak discovery, PKCE S256, redirect origin, audience
mapping, and live RS256 JWKS before starting the browser flow. Kong's
declarative signing-key configuration is generated from that run's JWKS.

On success, failure, or interruption, cleanup targets only the unique Compose
project: it removes that project's containers, network, and disposable
PostgreSQL volume, then removes only the API and Go image tags generated for
that run. It verifies the project has no remaining labeled containers,
networks, or volumes. Shared base images pulled by Docker are retained. The
runner never connects to or removes `finance_db`, `finance_test`, or any other
pre-existing database/container/volume.

Sanitized service logs, the Playwright log, and failure screenshots are retained
under `/tmp/pfp-live-e2e-artifacts/<run-id>` by default. OAuth traces remain
disabled because traces can contain bearer headers. Use `--artifacts PATH` to
choose a different artifact root.
