# Observability V1

TASK 16 adds observability without making logs or metrics part of financial truth.

## Failure modes covered

- A request fails and operators cannot correlate logs across the request.
- Database connectivity/schema is broken while the process is still alive.
- Provider sync repeatedly conflicts or fails.
- Reconciliation mismatches are being detected but not noticed operationally.
- Projection reads are stale/missing or rebuilds fail.
- API latency or 5xx rate changes without a visible signal.

## Request correlation

Every HTTP request gets an `X-Request-ID`.

A caller-supplied ID is accepted only when it matches a bounded safe character
set. Invalid or oversized values are replaced with a generated UUID. The same
ID is returned in the response and attached to structured request logs.

Request bodies, query strings, bearer tokens, raw provider payloads, passwords,
and secrets are not logged.

## Structured logs

Operational events are emitted as one-line JSON to stdout. Logs describe
transitions/failures using identifiers and event names, not financial values.

Examples:

- `http.request_completed`
- `http.request_failed`
- `database.readiness_failed`
- `provider_sync.checkpoint_conflict`
- `provider_sync.failed`
- `reconciliation.mismatch_detected`
- `projection.stale_read`
- `projection.rebuild_failed`

Container/platform logging should collect stdout in production.

## Health

`GET /health/live`

Process liveness only. It does not query dependencies.

`GET /health/ready`

Checks:
1. PostgreSQL connectivity.
2. Presence of critical migrated V1 tables.

It returns 503 when the process must not receive production traffic.

## Metrics

`GET /observability/metrics` requires normal Bearer authentication in V1.

Runtime counters are process-local and reset on process restart. They are
operational signals only and are never used to decide financial state.

Durable gauges are computed from PostgreSQL and expose counts/ages only, not
money values or provider payloads.

Notable runtime counters:

- `http_requests_total`
- `http_5xx_total`
- `db_errors_total`
- `provider_sync_checkpoint_conflicts_total`
- `provider_sync_failures_total`
- `provider_sync_runs_completed_total`
- `reconciliation_mismatches_detected_total`
- `projection_missing_reads_total`
- `projection_stale_reads_total`
- `projection_rebuild_failures_total`
- `projection_rebuilds_completed_total`

Durable gauges include:

- open reconciliation mismatch count
- provider checkpoint/page counts
- oldest checkpoint update age
- projection row count
- users currently missing a projection

The checkpoint age is deliberately **not** called "stuck". Until a real provider
sync cadence/SLA exists, age alone cannot prove a stuck worker.

## Multi-worker semantics

Runtime counters are per process. In a multi-worker deployment every worker
must be scraped/aggregated by the observability platform, or this adapter can
later be replaced by Prometheus/OpenTelemetry instrumentation.

This limitation does not affect business correctness because metrics are not
canonical state.

## Explicitly deferred

- Distributed tracing/OpenTelemetry exporter: no distributed topology exists yet.
- Alert thresholds: require production SLOs and provider sync cadence.
- External metrics backend: choose with deployment topology.
- Full log shipping stack: deployment concern, not application truth.
