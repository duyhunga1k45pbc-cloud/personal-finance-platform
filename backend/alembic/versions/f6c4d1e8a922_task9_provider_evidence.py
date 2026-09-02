"""task9 immutable provider evidence and ingestion identity

Revision ID: f6c4d1e8a922
Revises: e5b3c9d4f711
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f6c4d1e8a922"
down_revision = "e5b3c9d4f711"
branch_labels = None
depends_on = None


OLD_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', "
    "'CREATE_RECONCILIATION', 'RESOLVE_RECONCILIATION', "
    "'CONFIRM_RECONCILIATION_ADJUSTMENT')"
)
NEW_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', "
    "'CREATE_RECONCILIATION', 'RESOLVE_RECONCILIATION', "
    "'CONFIRM_RECONCILIATION_ADJUSTMENT', 'CREATE_PROVIDER_CONNECTION', "
    "'INGEST_EXTERNAL_EVIDENCE')"
)
OLD_TARGET_CHECK = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL AND reconciliation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NOT NULL)"
)
NEW_TARGET_CHECK = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NOT NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NOT NULL AND external_evidence_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NOT NULL)"
)


def _create_postgres_immutable_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_external_transaction_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'external_transactions identity is immutable';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_external_transactions_immutable
        BEFORE UPDATE OR DELETE ON external_transactions
        FOR EACH ROW
        EXECUTE FUNCTION reject_external_transaction_mutation();
    """))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_external_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'external_transaction_evidence is immutable';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_external_evidence_immutable
        BEFORE UPDATE OR DELETE ON external_transaction_evidence
        FOR EACH ROW
        EXECUTE FUNCTION reject_external_evidence_mutation();
    """))


def _drop_postgres_immutable_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_external_evidence_immutable ON external_transaction_evidence"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_external_evidence_mutation()"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_external_transactions_immutable ON external_transactions"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_external_transaction_mutation()"))


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("external_account_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider_name",
            "external_account_id",
            name="uq_provider_connections_user_provider_account",
        ),
    )
    op.create_index(op.f("ix_provider_connections_user_id"), "provider_connections", ["user_id"], unique=False)

    op.create_table(
        "external_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("external_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_connection_id",
            "external_transaction_id",
            name="uq_external_transactions_connection_external_id",
        ),
    )
    op.create_index(op.f("ix_external_transactions_user_id"), "external_transactions", ["user_id"], unique=False)
    op.create_index(op.f("ix_external_transactions_provider_connection_id"), "external_transactions", ["provider_connection_id"], unique=False)

    op.create_table(
        "external_transaction_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("external_transaction_record_id", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("char_length(payload_sha256) = 64", name="ck_external_evidence_sha256_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.ForeignKeyConstraint(["external_transaction_record_id"], ["external_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_transaction_record_id",
            "payload_sha256",
            "observed_at",
            name="uq_external_evidence_exact_observation",
        ),
    )
    op.create_index(op.f("ix_external_transaction_evidence_user_id"), "external_transaction_evidence", ["user_id"], unique=False)
    op.create_index(op.f("ix_external_transaction_evidence_provider_connection_id"), "external_transaction_evidence", ["provider_connection_id"], unique=False)
    op.create_index(op.f("ix_external_transaction_evidence_external_transaction_record_id"), "external_transaction_evidence", ["external_transaction_record_id"], unique=False)

    _create_postgres_immutable_triggers()

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.add_column(sa.Column("provider_connection_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("external_evidence_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_command_receipts_provider_connection_id",
                "provider_connections",
                ["provider_connection_id"],
                ["id"],
            )
            batch.create_foreign_key(
                "fk_command_receipts_external_evidence_id",
                "external_transaction_evidence",
                ["external_evidence_id"],
                ["id"],
            )
            batch.create_check_constraint("ck_command_receipts_command_type", NEW_COMMAND_TYPES)
            batch.create_check_constraint("ck_command_receipts_exactly_one_target", NEW_TARGET_CHECK)
    else:
        op.add_column("command_receipts", sa.Column("provider_connection_id", sa.Integer(), nullable=True))
        op.add_column("command_receipts", sa.Column("external_evidence_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_command_receipts_provider_connection_id",
            "command_receipts",
            "provider_connections",
            ["provider_connection_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_command_receipts_external_evidence_id",
            "command_receipts",
            "external_transaction_evidence",
            ["external_evidence_id"],
            ["id"],
        )
        op.drop_constraint("ck_command_receipts_command_type", "command_receipts", type_="check")
        op.drop_constraint("ck_command_receipts_exactly_one_target", "command_receipts", type_="check")
        op.create_check_constraint("ck_command_receipts_command_type", "command_receipts", NEW_COMMAND_TYPES)
        op.create_check_constraint("ck_command_receipts_exactly_one_target", "command_receipts", NEW_TARGET_CHECK)

    op.create_index(op.f("ix_command_receipts_provider_connection_id"), "command_receipts", ["provider_connection_id"], unique=False)
    op.create_index(op.f("ix_command_receipts_external_evidence_id"), "command_receipts", ["external_evidence_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    provider_count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_connections")).scalar()
    receipt_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type IN "
            "('CREATE_PROVIDER_CONNECTION', 'INGEST_EXTERNAL_EVIDENCE')"
        )
    ).scalar()
    if provider_count or receipt_count:
        raise RuntimeError(
            "Cannot downgrade Task 9 while provider evidence exists. "
            "Downgrade would discard immutable source evidence."
        )

    op.drop_index(op.f("ix_command_receipts_external_evidence_id"), table_name="command_receipts")
    op.drop_index(op.f("ix_command_receipts_provider_connection_id"), table_name="command_receipts")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.drop_constraint("fk_command_receipts_external_evidence_id", type_="foreignkey")
            batch.drop_constraint("fk_command_receipts_provider_connection_id", type_="foreignkey")
            batch.drop_column("external_evidence_id")
            batch.drop_column("provider_connection_id")
            batch.create_check_constraint("ck_command_receipts_command_type", OLD_COMMAND_TYPES)
            batch.create_check_constraint("ck_command_receipts_exactly_one_target", OLD_TARGET_CHECK)
    else:
        op.drop_constraint("ck_command_receipts_command_type", "command_receipts", type_="check")
        op.drop_constraint("ck_command_receipts_exactly_one_target", "command_receipts", type_="check")
        op.drop_constraint("fk_command_receipts_external_evidence_id", "command_receipts", type_="foreignkey")
        op.drop_constraint("fk_command_receipts_provider_connection_id", "command_receipts", type_="foreignkey")
        op.drop_column("command_receipts", "external_evidence_id")
        op.drop_column("command_receipts", "provider_connection_id")
        op.create_check_constraint("ck_command_receipts_command_type", "command_receipts", OLD_COMMAND_TYPES)
        op.create_check_constraint("ck_command_receipts_exactly_one_target", "command_receipts", OLD_TARGET_CHECK)

    _drop_postgres_immutable_triggers()

    op.drop_index(op.f("ix_external_transaction_evidence_external_transaction_record_id"), table_name="external_transaction_evidence")
    op.drop_index(op.f("ix_external_transaction_evidence_provider_connection_id"), table_name="external_transaction_evidence")
    op.drop_index(op.f("ix_external_transaction_evidence_user_id"), table_name="external_transaction_evidence")
    op.drop_table("external_transaction_evidence")

    op.drop_index(op.f("ix_external_transactions_provider_connection_id"), table_name="external_transactions")
    op.drop_index(op.f("ix_external_transactions_user_id"), table_name="external_transactions")
    op.drop_table("external_transactions")

    op.drop_index(op.f("ix_provider_connections_user_id"), table_name="provider_connections")
    op.drop_table("provider_connections")
