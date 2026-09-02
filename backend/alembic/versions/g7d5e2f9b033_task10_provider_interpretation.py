"""task10 provider normalization and interpretation state machine

Revision ID: g7d5e2f9b033
Revises: f6c4d1e8a922
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "g7d5e2f9b033"
down_revision = "f6c4d1e8a922"
branch_labels = None
depends_on = None

OLD_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', 'DELETE_TRANSACTION', "
    "'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', 'CREATE_RECONCILIATION', "
    "'RESOLVE_RECONCILIATION', 'CONFIRM_RECONCILIATION_ADJUSTMENT', "
    "'CREATE_PROVIDER_CONNECTION', 'INGEST_EXTERNAL_EVIDENCE')"
)
NEW_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', 'DELETE_TRANSACTION', "
    "'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', 'CREATE_RECONCILIATION', "
    "'RESOLVE_RECONCILIATION', 'CONFIRM_RECONCILIATION_ADJUSTMENT', "
    "'CREATE_PROVIDER_CONNECTION', 'INGEST_EXTERNAL_EVIDENCE', "
    "'NORMALIZE_EXTERNAL_TRANSACTION', 'CLASSIFY_EXTERNAL_TRANSACTION', "
    "'CONFIRM_EXTERNAL_INTERPRETATION')"
)

OLD_TARGET_CHECK = (
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

NEW_TARGET_CHECK = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NOT NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NOT NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NOT NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NOT NULL AND provider_interpretation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL "
    "AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NOT NULL)"
)


def _immutable_trigger(table: str, trigger: str, function: str, message: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"""
        CREATE OR REPLACE FUNCTION {function}()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '{message}';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text(f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW
        EXECUTE FUNCTION {function}();
    """))


def _drop_immutable_trigger(table: str, trigger: str, function: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))


def upgrade() -> None:
    op.create_table(
        "provider_normalized_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("external_transaction_record_id", sa.Integer(), nullable=False),
        sa.Column("source_evidence_id", sa.Integer(), nullable=False),
        sa.Column("normalizer_version", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("normalized_status", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("provider_status", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("amount > 0", name="ck_provider_normalized_candidates_amount_positive"),
        sa.CheckConstraint("direction IN ('INFLOW', 'OUTFLOW')", name="ck_provider_normalized_candidates_direction"),
        sa.CheckConstraint("normalized_status IN ('PENDING', 'POSTED', 'REVERSED', 'UNKNOWN')", name="ck_provider_normalized_candidates_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.ForeignKeyConstraint(["external_transaction_record_id"], ["external_transactions.id"]),
        sa.ForeignKeyConstraint(["source_evidence_id"], ["external_transaction_evidence.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_evidence_id", "normalizer_version", name="uq_provider_normalized_candidate_evidence_version"),
    )
    op.create_index(op.f("ix_provider_normalized_candidates_user_id"), "provider_normalized_candidates", ["user_id"], unique=False)
    op.create_index(op.f("ix_provider_normalized_candidates_external_transaction_record_id"), "provider_normalized_candidates", ["external_transaction_record_id"], unique=False)
    op.create_index(op.f("ix_provider_normalized_candidates_source_evidence_id"), "provider_normalized_candidates", ["source_evidence_id"], unique=False)

    op.create_table(
        "provider_transaction_interpretations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("external_transaction_record_id", sa.Integer(), nullable=False),
        sa.Column("normalized_candidate_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("canonical_event_id", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("state IN ('UNCLASSIFIED', 'CLASSIFIED', 'USER_CONFIRMED')", name="ck_provider_interpretations_state"),
        sa.CheckConstraint("event_type IS NULL OR event_type IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REFUND', 'REVERSAL')", name="ck_provider_interpretations_event_type"),
        sa.CheckConstraint("confidence IS NULL OR confidence IN ('INFERRED', 'USER_CONFIRMED')", name="ck_provider_interpretations_confidence"),
        sa.CheckConstraint("version >= 1", name="ck_provider_interpretations_version_positive"),
        sa.CheckConstraint(
            "(state = 'UNCLASSIFIED' AND event_type IS NULL AND account_id IS NULL AND confidence IS NULL AND canonical_event_id IS NULL) OR "
            "(state = 'CLASSIFIED' AND event_type IS NOT NULL AND account_id IS NULL AND confidence = 'INFERRED' AND canonical_event_id IS NULL) OR "
            "(state = 'USER_CONFIRMED' AND event_type IS NOT NULL AND account_id IS NOT NULL AND confidence = 'USER_CONFIRMED')",
            name="ck_provider_interpretations_state_shape",
        ),
        sa.CheckConstraint(
            "canonical_event_id IS NULL OR (state = 'USER_CONFIRMED' AND event_type IN ('INCOME', 'EXPENSE'))",
            name="ck_provider_interpretations_canonical_materialization_scope",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["external_transaction_record_id"], ["external_transactions.id"]),
        sa.ForeignKeyConstraint(["normalized_candidate_id"], ["provider_normalized_candidates.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["financial_accounts.id"]),
        sa.ForeignKeyConstraint(["canonical_event_id"], ["financial_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_transaction_record_id", name="uq_provider_interpretations_external_transaction"),
        sa.UniqueConstraint("canonical_event_id", name="uq_provider_interpretations_canonical_event"),
    )
    op.create_index(op.f("ix_provider_transaction_interpretations_user_id"), "provider_transaction_interpretations", ["user_id"], unique=False)

    op.create_table(
        "provider_interpretation_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("interpretation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("interpretation_version", sa.Integer(), nullable=False),
        sa.Column("transition_type", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("previous_state", sa.JSON(), nullable=True),
        sa.Column("new_state", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("interpretation_version >= 1", name="ck_provider_interpretation_history_version_positive"),
        sa.CheckConstraint("transition_type IN ('NORMALIZED', 'CLASSIFIED', 'USER_CONFIRMED', 'MATERIALIZED')", name="ck_provider_interpretation_history_transition_type"),
        sa.CheckConstraint("actor_type IN ('SYSTEM', 'USER')", name="ck_provider_interpretation_history_actor_type"),
        sa.ForeignKeyConstraint(["interpretation_id"], ["provider_transaction_interpretations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interpretation_id", "interpretation_version", name="uq_provider_interpretation_history_version"),
    )
    op.create_index(op.f("ix_provider_interpretation_history_interpretation_id"), "provider_interpretation_history", ["interpretation_id"], unique=False)

    _immutable_trigger(
        "provider_normalized_candidates",
        "trg_provider_normalized_candidates_immutable",
        "reject_provider_normalized_candidate_mutation",
        "provider normalized candidates are immutable",
    )
    _immutable_trigger(
        "provider_interpretation_history",
        "trg_provider_interpretation_history_immutable",
        "reject_provider_interpretation_history_mutation",
        "provider interpretation history is append-only",
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.add_column(sa.Column("normalized_candidate_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("provider_interpretation_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_command_receipts_normalized_candidate_id", "provider_normalized_candidates", ["normalized_candidate_id"], ["id"])
            batch.create_foreign_key("fk_command_receipts_provider_interpretation_id", "provider_transaction_interpretations", ["provider_interpretation_id"], ["id"])
            batch.create_check_constraint("ck_command_receipts_command_type", NEW_COMMAND_TYPES)
            batch.create_check_constraint("ck_command_receipts_exactly_one_target", NEW_TARGET_CHECK)
    else:
        op.add_column("command_receipts", sa.Column("normalized_candidate_id", sa.Integer(), nullable=True))
        op.add_column("command_receipts", sa.Column("provider_interpretation_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_command_receipts_normalized_candidate_id", "command_receipts", "provider_normalized_candidates", ["normalized_candidate_id"], ["id"])
        op.create_foreign_key("fk_command_receipts_provider_interpretation_id", "command_receipts", "provider_transaction_interpretations", ["provider_interpretation_id"], ["id"])
        op.drop_constraint("ck_command_receipts_command_type", "command_receipts", type_="check")
        op.drop_constraint("ck_command_receipts_exactly_one_target", "command_receipts", type_="check")
        op.create_check_constraint("ck_command_receipts_command_type", "command_receipts", NEW_COMMAND_TYPES)
        op.create_check_constraint("ck_command_receipts_exactly_one_target", "command_receipts", NEW_TARGET_CHECK)

    op.create_index(op.f("ix_command_receipts_normalized_candidate_id"), "command_receipts", ["normalized_candidate_id"], unique=False)
    op.create_index(op.f("ix_command_receipts_provider_interpretation_id"), "command_receipts", ["provider_interpretation_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_transaction_interpretations")).scalar()
    history_count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_interpretation_history")).scalar()
    candidate_count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_normalized_candidates")).scalar()
    if count or history_count or candidate_count:
        raise RuntimeError(
            "Cannot downgrade Task 10 while normalized/provider interpretation state exists; "
            "downgrade would discard user/system semantic decisions."
        )

    op.drop_index(op.f("ix_command_receipts_provider_interpretation_id"), table_name="command_receipts")
    op.drop_index(op.f("ix_command_receipts_normalized_candidate_id"), table_name="command_receipts")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.drop_constraint("fk_command_receipts_provider_interpretation_id", type_="foreignkey")
            batch.drop_constraint("fk_command_receipts_normalized_candidate_id", type_="foreignkey")
            batch.drop_column("provider_interpretation_id")
            batch.drop_column("normalized_candidate_id")
            batch.create_check_constraint("ck_command_receipts_command_type", OLD_COMMAND_TYPES)
            batch.create_check_constraint("ck_command_receipts_exactly_one_target", OLD_TARGET_CHECK)
    else:
        op.drop_constraint("ck_command_receipts_command_type", "command_receipts", type_="check")
        op.drop_constraint("ck_command_receipts_exactly_one_target", "command_receipts", type_="check")
        op.drop_constraint("fk_command_receipts_provider_interpretation_id", "command_receipts", type_="foreignkey")
        op.drop_constraint("fk_command_receipts_normalized_candidate_id", "command_receipts", type_="foreignkey")
        op.drop_column("command_receipts", "provider_interpretation_id")
        op.drop_column("command_receipts", "normalized_candidate_id")
        op.create_check_constraint("ck_command_receipts_command_type", "command_receipts", OLD_COMMAND_TYPES)
        op.create_check_constraint("ck_command_receipts_exactly_one_target", "command_receipts", OLD_TARGET_CHECK)

    _drop_immutable_trigger("provider_interpretation_history", "trg_provider_interpretation_history_immutable", "reject_provider_interpretation_history_mutation")
    _drop_immutable_trigger("provider_normalized_candidates", "trg_provider_normalized_candidates_immutable", "reject_provider_normalized_candidate_mutation")

    op.drop_index(op.f("ix_provider_interpretation_history_interpretation_id"), table_name="provider_interpretation_history")
    op.drop_table("provider_interpretation_history")
    op.drop_index(op.f("ix_provider_transaction_interpretations_user_id"), table_name="provider_transaction_interpretations")
    op.drop_table("provider_transaction_interpretations")
    op.drop_index(op.f("ix_provider_normalized_candidates_source_evidence_id"), table_name="provider_normalized_candidates")
    op.drop_index(op.f("ix_provider_normalized_candidates_external_transaction_record_id"), table_name="provider_normalized_candidates")
    op.drop_index(op.f("ix_provider_normalized_candidates_user_id"), table_name="provider_normalized_candidates")
    op.drop_table("provider_normalized_candidates")
