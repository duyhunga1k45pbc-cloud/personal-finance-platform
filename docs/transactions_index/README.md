# Phase A: deterministic `GET /transactions` pagination + `(user_id, id)` index

## Ordering decision
`ORDER BY transactions.id ASC` (unique, so a complete tie-breaker).
- Preserves the de-facto order clients already saw (insertion/id order); no response-schema change.
- `Transaction.date` is server-assigned (`default=utcnow`, not in `TransactionCreate`), so `(date, id)` adds no
  information and would require a different index. DESC (newest-first) would be a breaking change (Moneta
  requests `limit=100`) and is deliberately out of scope.

## Original problem
The query had no ORDER BY. Same data, same offset, different plan => different rows
(audit DB, user 5, `OFFSET 5000 LIMIT 5`):
- default (merge join): 134547..134551
- hash join: 131504..131508
- ordered by id: 135099..135103 (any plan)
(The unordered rows differ even from each other; ordered output is plan-independent.)
Small-data tests pass before and after the fix because the planner happens to return id order there.

## Plans / benchmarks (498k transactions, skewed users; `bench.py`, median of 9 runs, ms)
Run: `DATABASE_URL=.../finance_index_audit python docs/transactions_index/bench.py [--runs 9] [--plans]`
(seed: `docs/index_audit/seed.sql` on branch `perf/index-audit`).

| Case | Query | Before (no user_id index) | After `(user_id,id)` |
|---|---|---|---|
| light (100 rows) p1 | count | 10.78 | 0.11 |
| light p1 | page | 21.93 | 0.02 |
| light skip 50 | page | 10.21 | 0.07 |
| heavy (10k) p1 | count | 39.14 | 29.67 |
| heavy p1 | page | 27.24 | 19.24 |
| heavy skip 5000 | page | 36.77 (Seq Scan) | 20.80 (Index Scan) |
| whale (100k) p1 | page | 0.02 | 0.02 |
| whale skip 50000 | page | 17.66 | 17.60 |
| whale type=expense p1 | page | 0.02 | 0.03 |

Buffers: light count 5420 -> 404; heavy page ~13.6k -> ~2.6k. Whale count stays ~41 ms (still scans
all `financial_events`: count remains O(table) via the join; not addressed here).
Index size 11 MB (table 39 MB). Write cost measured in the audit: about +10% insert time (+14 ms / 20k rows).

## Migration
`l2c0d7e4a588`: `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transactions_user_id_id` inside
`autocommit_block()`; an INVALID leftover from an interrupted build is dropped first.
Downgrade: `DROP INDEX CONCURRENTLY IF EXISTS`. Verified upgrade -> downgrade -> upgrade -> upgrade
(idempotent) on a disposable DB; index `indisvalid = t`. The model declares the same Index.

## Known limitations
- Deterministic ORDER BY does not prevent drift when rows are inserted between page requests
  (separate snapshots). Cursor pagination is out of scope.
- id ASC means `limit=100` shows the oldest 100 for large accounts (unchanged behavior).
- Large offsets and whale-user counts remain costly; the four duplicate indexes are untouched.
- `alembic check` reports 4 pre-existing unrelated missing-index diffs (not introduced here).
