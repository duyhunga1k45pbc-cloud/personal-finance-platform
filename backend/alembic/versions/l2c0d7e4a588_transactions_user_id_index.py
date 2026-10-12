"""index transactions by (user_id, id) for ownership-scoped, id-ordered listing

Revision ID: l2c0d7e4a588
Revises: k1b9c6d3f477
Create Date: 2026-10-12
"""

from alembic import op


revision = "l2c0d7e4a588"
down_revision = "k1b9c6d3f477"
branch_labels = None
depends_on = None

INDEX = "ix_transactions_user_id_id"


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block; autocommit_block() commits the
    # migration transaction, runs the DDL outside it, and resumes. IF NOT EXISTS makes a retry
    # after an interrupted build safe, but an interrupted build leaves an INVALID index that
    # must be dropped first (see docs/transactions_index/README.md).
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}") if _invalid() else None
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON transactions (user_id, id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")


def _invalid() -> bool:
    row = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        f"WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
    ).first()
    return row is not None
