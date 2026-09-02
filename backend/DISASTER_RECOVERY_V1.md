# V1 Disaster Recovery Contract

TASK 17 treats backup/restore as a correctness mechanism, not as a file-copy checkbox.

## Failure being contained

A database can be lost or corrupted even when application-level financial semantics are correct. Recovery must preserve the evidence, canonical state, histories, idempotency records, provider checkpoints, reconciliation state, schema constraints/triggers, and the ability to rebuild derived projections.

## Recovery contract

A V1 backup is valid only when all of the following are true:

1. `pg_dump` uses PostgreSQL custom format.
2. The dump and integrity manifest refer to the **same exported PostgreSQL snapshot**.
3. The manifest records the Alembic revision, every public-table row count/hash, schema-object hash, complete database hash, and a hash of durable truth excluding disposable projections.
4. The dump SHA-256 matches the manifest before restore.
5. A restore drill recreates a disposable database and verifies the exact restored database fingerprint.
6. Projection rows can then be deleted, rebuilt from canonical state, and canonical audit still passes.
7. The non-projection truth fingerprint is identical before and after projection rebuild.

The restore drill never writes to the source database.

## Safety boundary

`RESTORE_DATABASE_URL` is destructive. The V1 tool refuses targets unless the database name:

- differs from the source database;
- contains `restore_test` or `dr_test`;
- contains only letters, digits, and underscores.

This is deliberate. Production disaster restoration should be an explicit operator procedure, not an accidentally reusable test command.

## Backup

From `backend/`:

```bash
DATABASE_URL=postgresql://... \
python -m scripts.backup_database --output-dir /secure/backup/path
```

A successful backup publishes two files:

```text
<database>-<timestamp>.dump
<database>-<timestamp>.manifest.json
```

The manifest contains no database password or full connection URL.

## Real restore drill

Use a disposable database such as `finance_dr_test`:

```bash
DATABASE_URL=postgresql://postgres:***@localhost:5432/finance_test_db \
RESTORE_DATABASE_URL=postgresql://postgres:***@localhost:5432/finance_dr_test \
python -m scripts.verify_backup_restore
```

Definition of done:

```text
restore_exact_match=true
projection_rebuild_verified=true
canonical_audit_ok=true
```

The script also prints `recovery_seconds`; this is observed drill duration, not a promised RTO.

## RPO and RTO

V1 does not invent production SLOs without a deployment/business requirement.

- **RPO (Recovery Point Objective):** maximum acceptable data loss. Backup cadence must be no longer than the chosen RPO. For smaller RPOs, scheduled base backups plus PostgreSQL WAL archiving / point-in-time recovery are the next justified mechanism.
- **RTO (Recovery Time Objective):** maximum acceptable recovery time. TASK 17 measures actual drill duration. A target should only be declared after deployment size and business tolerance are known.

A nightly dump does **not** mean zero-data-loss recovery. It implies up to roughly one backup interval of data loss if no WAL/PITR layer exists.

## What this task intentionally does not add

- S3/object-storage upload policy
- encryption/KMS policy
- WAL archiving / PITR
- cross-region replication
- automated production database destruction/restoration

Those mechanisms need concrete RPO/RTO, hosting, retention, and key-management requirements. TASK 17 creates the recovery correctness boundary first.
