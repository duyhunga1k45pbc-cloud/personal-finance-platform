# V1 Deployment and Failure Operations

TASK 18 closes the V1 operational-correctness loop. It does not choose Kubernetes,
ECS, Nomad, systemd, or another topology. The application exposes deployment
invariants that any of those runtimes can enforce.

## Deployment invariants

1. **No implicit migration on API startup.** Database migration is a distinct
   release step. Multiple API replicas must not race to mutate schema.
2. **A release serves traffic only when PostgreSQL is reachable and the current
   Alembic revision equals the code's migration head.** Stale code/schema pairs
   fail closed.
3. **Liveness and readiness are different.** `/health/live` means the process is
   alive. `/health/ready` means the instance is safe to receive traffic.
4. **Canonical audit can gate a release.** Deployment tooling can refuse a
   rollout when durable financial truth already violates an invariant.
5. **Shutdown is graceful, bounded, and observable.** The production launcher
   configures Uvicorn's graceful-shutdown timeout; application shutdown marks
   the process as draining.
6. **Rollback never blindly downgrades financial schema.** Database downgrade is
   not an automatic rollback mechanism. Roll back application code only when
   the deployed migration is explicitly backward compatible; otherwise prefer
   a forward fix.
7. **Release metadata is observational only.** `APP_RELEASE` and `GIT_SHA` help
   identify a running build but never affect financial state.

## Release sequence

A V1 release should be operated in this order:

```text
verified backup / recovery capability
        ↓
alembic upgrade head
        ↓
python -m scripts.deployment_preflight
        ↓
start new application release
        ↓
/health/live = 200
/health/ready = 200
        ↓
python -m scripts.deployment_smoke --base-url ...
        ↓
observe 5xx, DB errors, reconciliation and projection signals
```

The application never runs `alembic upgrade head` automatically. Schema change
and traffic-serving are separate state transitions with separate failure modes.

## Production launcher

Required configuration includes the TASK 15 production security settings and
`DATABASE_URL`.

Example:

```bash
APP_ENV=production \
SECRET_KEY='<strong secret>' \
DATABASE_URL='postgresql://...' \
APP_RELEASE='v1.0.0' \
GIT_SHA='<commit>' \
python -m scripts.run_production
```

Optional process settings:

- `API_HOST` (default `0.0.0.0`)
- `API_PORT` (default `8000`)
- `API_WORKERS` (default `1`)
- `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` (default `30`)

Runtime metrics in TASK 16 are process-local. With multiple workers, a collector
must scrape/aggregate every worker; business correctness must never depend on
those counters.

## Preflight

```bash
python -m scripts.deployment_preflight
```

Preflight checks:

- DB connectivity and critical tables;
- current Alembic revision equals migration head;
- canonical audit is green for all users.

It exits non-zero on failure. `--skip-canonical-audit` exists for diagnostics,
not as the preferred production release path.

## Smoke check

```bash
python -m scripts.deployment_smoke --base-url http://127.0.0.1:8000
```

Smoke requires both liveness and readiness to be healthy. It does not create or
modify financial state.

## Failure semantics

| Failure | Expected behavior |
|---|---|
| PostgreSQL unavailable before startup | preflight/startup fails; release does not serve |
| Code ahead of DB migration | readiness 503; startup gate refuses release |
| Process alive, DB becomes unavailable | liveness may remain 200; readiness becomes 503 |
| Canonical audit fails | release preflight exits non-zero |
| New release smoke fails | stop rollout; do not mutate truth to make smoke pass |
| SIGTERM/shutdown | Uvicorn drains in-flight work up to configured timeout |
| Need rollback after schema migration | app rollback only if schema compatibility was established; otherwise forward-fix |

## What TASK 18 intentionally does not add

There is no Kubernetes YAML, load balancer, service mesh, distributed lock,
blue/green controller, or cloud-specific deployment service. Those mechanisms
need a concrete deployment topology and availability requirement. V1 provides
the application-level contracts they need: startup gate, readiness, preflight,
smoke verification, graceful shutdown, observability, and verified recovery.
