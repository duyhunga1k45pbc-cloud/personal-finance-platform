"""provider listing committed-data revision

Revision ID: k1b9c6d3f477
Revises: j0a8b5c2e366
Create Date: 2026-10-12
"""

from alembic import op
import sqlalchemy as sa


revision = "k1b9c6d3f477"
down_revision = "j0a8b5c2e366"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_listing_revisions",
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.PrimaryKeyConstraint("provider_connection_id"),
    )
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # The bump runs in the writer's own transaction, so it commits or rolls back
    # (including savepoints) together with the inserted rows.
    for table in ("external_transactions", "external_transaction_evidence"):
        op.execute(sa.text(f"""
            CREATE OR REPLACE FUNCTION bump_listing_revision_{table}()
            RETURNS trigger AS $$
            BEGIN
                INSERT INTO provider_listing_revisions (provider_connection_id, revision)
                SELECT provider_connection_id, 1
                FROM new_rows
                GROUP BY provider_connection_id
                ORDER BY provider_connection_id
                ON CONFLICT (provider_connection_id)
                DO UPDATE SET revision = provider_listing_revisions.revision + 1;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """))
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table}_listing_revision
            AFTER INSERT ON {table}
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT
            EXECUTE FUNCTION bump_listing_revision_{table}();
        """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("external_transaction_evidence", "external_transactions"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_listing_revision ON {table}"))
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS bump_listing_revision_{table}()"))
    op.drop_table("provider_listing_revisions")
