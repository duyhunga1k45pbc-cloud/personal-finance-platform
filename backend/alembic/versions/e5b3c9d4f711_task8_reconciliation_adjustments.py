"""task8 reconciliation and explicit adjustments

Revision ID: e5b3c9d4f711
Revises: d4a9b7c2e810
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "e5b3c9d4f711"
down_revision = "d4a9b7c2e810"
branch_labels = None
depends_on = None


OLD_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL')"
)
NEW_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', "
    "'CREATE_RECONCILIATION', 'RESOLVE_RECONCILIATION', "
    "'CONFIRM_RECONCILIATION_ADJUSTMENT')"
)
OLD_TARGET_CHECK = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL)"
)
NEW_TARGET_CHECK = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL AND reconciliation_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NOT NULL)"
)


def _create_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_reconciliation_history_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'reconciliation_history is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_reconciliation_history_append_only
            BEFORE UPDATE OR DELETE ON reconciliation_history
            FOR EACH ROW
            EXECUTE FUNCTION reject_reconciliation_history_mutation();
            """
        )
    )


def _drop_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_reconciliation_history_append_only "
            "ON reconciliation_history"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS reject_reconciliation_history_mutation()")
    )


def upgrade() -> None:
    op.create_table(
        "reconciliation_cases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("expected_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("observed_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("difference", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolution_type", sa.String(), nullable=True),
        sa.Column("adjustment_event_id", sa.Integer(), nullable=True),
        sa.Column("resolved_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('UNKNOWN', 'RECONCILED', 'MISMATCH', 'RESOLVED')",
            name="ck_reconciliation_cases_status",
        ),
        sa.CheckConstraint(
            "resolution_type IS NULL OR resolution_type IN ('REAL_EVENT', 'ADJUSTMENT')",
            name="ck_reconciliation_cases_resolution_type",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_reconciliation_cases_version_positive",
        ),
        sa.CheckConstraint(
            "difference = observed_balance - expected_balance",
            name="ck_reconciliation_cases_difference_exact",
        ),
        sa.CheckConstraint(
            "status <> 'RECONCILED' OR difference = 0",
            name="ck_reconciliation_cases_reconciled_zero_difference",
        ),
        sa.CheckConstraint(
            "status <> 'MISMATCH' OR difference <> 0",
            name="ck_reconciliation_cases_mismatch_nonzero_difference",
        ),
        sa.CheckConstraint(
            "(status = 'RESOLVED' AND resolution_type IS NOT NULL AND resolved_balance IS NOT NULL) OR "
            "(status <> 'RESOLVED' AND resolution_type IS NULL AND resolved_balance IS NULL)",
            name="ck_reconciliation_cases_resolution_state",
        ),
        sa.CheckConstraint(
            "(resolution_type = 'ADJUSTMENT' AND adjustment_event_id IS NOT NULL) OR "
            "(COALESCE(resolution_type, '') <> 'ADJUSTMENT' AND adjustment_event_id IS NULL)",
            name="ck_reconciliation_cases_adjustment_target",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["financial_accounts.id"]),
        sa.ForeignKeyConstraint(["adjustment_event_id"], ["financial_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "adjustment_event_id",
            name="uq_reconciliation_cases_adjustment_event_id",
        ),
    )
    op.create_index(
        op.f("ix_reconciliation_cases_user_id"),
        "reconciliation_cases",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reconciliation_cases_account_id"),
        "reconciliation_cases",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reconciliation_cases_adjustment_event_id"),
        "reconciliation_cases",
        ["adjustment_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_cases_user_status",
        "reconciliation_cases",
        ["user_id", "status"],
        unique=False,
    )

    op.create_table(
        "reconciliation_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reconciliation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reconciliation_version", sa.Integer(), nullable=False),
        sa.Column("transition_type", sa.String(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("previous_state", sa.JSON(), nullable=True),
        sa.Column("new_state", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "transition_type IN ('DETECTED', 'RESOLVED_REAL_EVENT', 'RESOLVED_ADJUSTMENT')",
            name="ck_reconciliation_history_transition_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'PROVIDER', 'AI')",
            name="ck_reconciliation_history_actor_type",
        ),
        sa.CheckConstraint(
            "reconciliation_version >= 1",
            name="ck_reconciliation_history_version_positive",
        ),
        sa.ForeignKeyConstraint(["reconciliation_id"], ["reconciliation_cases.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reconciliation_id",
            "reconciliation_version",
            name="uq_reconciliation_history_case_version",
        ),
    )
    op.create_index(
        op.f("ix_reconciliation_history_reconciliation_id"),
        "reconciliation_history",
        ["reconciliation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reconciliation_history_user_id"),
        "reconciliation_history",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_history_case_recorded",
        "reconciliation_history",
        ["reconciliation_id", "recorded_at"],
        unique=False,
    )
    _create_postgres_append_only_trigger()

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.add_column(sa.Column("reconciliation_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_command_receipts_reconciliation_id",
                "reconciliation_cases",
                ["reconciliation_id"],
                ["id"],
            )
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                NEW_COMMAND_TYPES,
            )
            batch.create_check_constraint(
                "ck_command_receipts_exactly_one_target",
                NEW_TARGET_CHECK,
            )
    else:
        op.add_column(
            "command_receipts",
            sa.Column("reconciliation_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_command_receipts_reconciliation_id",
            "command_receipts",
            "reconciliation_cases",
            ["reconciliation_id"],
            ["id"],
        )
        op.drop_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            type_="check",
        )
        op.drop_constraint(
            "ck_command_receipts_exactly_one_target",
            "command_receipts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            NEW_COMMAND_TYPES,
        )
        op.create_check_constraint(
            "ck_command_receipts_exactly_one_target",
            "command_receipts",
            NEW_TARGET_CHECK,
        )
    op.create_index(
        op.f("ix_command_receipts_reconciliation_id"),
        "command_receipts",
        ["reconciliation_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    case_count = bind.execute(sa.text("SELECT COUNT(*) FROM reconciliation_cases")).scalar()
    receipt_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM command_receipts WHERE command_type IN "
            "('CREATE_RECONCILIATION', 'RESOLVE_RECONCILIATION', "
            "'CONFIRM_RECONCILIATION_ADJUSTMENT')"
        )
    ).scalar()
    adjustment_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM financial_events WHERE event_type = 'ADJUSTMENT'")
    ).scalar()
    if case_count or receipt_count or adjustment_count:
        raise RuntimeError(
            "Cannot downgrade Task 8 while reconciliation/adjustment state exists. "
            "Downgrade would discard reconciliation evidence."
        )

    op.drop_index(
        op.f("ix_command_receipts_reconciliation_id"),
        table_name="command_receipts",
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.drop_constraint("ck_command_receipts_exactly_one_target", type_="check")
            batch.drop_constraint("fk_command_receipts_reconciliation_id", type_="foreignkey")
            batch.drop_column("reconciliation_id")
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                OLD_COMMAND_TYPES,
            )
            batch.create_check_constraint(
                "ck_command_receipts_exactly_one_target",
                OLD_TARGET_CHECK,
            )
    else:
        op.drop_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            type_="check",
        )
        op.drop_constraint(
            "ck_command_receipts_exactly_one_target",
            "command_receipts",
            type_="check",
        )
        op.drop_constraint(
            "fk_command_receipts_reconciliation_id",
            "command_receipts",
            type_="foreignkey",
        )
        op.drop_column("command_receipts", "reconciliation_id")
        op.create_check_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            OLD_COMMAND_TYPES,
        )
        op.create_check_constraint(
            "ck_command_receipts_exactly_one_target",
            "command_receipts",
            OLD_TARGET_CHECK,
        )

    _drop_postgres_append_only_trigger()
    op.drop_index(
        "ix_reconciliation_history_case_recorded",
        table_name="reconciliation_history",
    )
    op.drop_index(
        op.f("ix_reconciliation_history_user_id"),
        table_name="reconciliation_history",
    )
    op.drop_index(
        op.f("ix_reconciliation_history_reconciliation_id"),
        table_name="reconciliation_history",
    )
    op.drop_table("reconciliation_history")

    op.drop_index(
        "ix_reconciliation_cases_user_status",
        table_name="reconciliation_cases",
    )
    op.drop_index(
        op.f("ix_reconciliation_cases_adjustment_event_id"),
        table_name="reconciliation_cases",
    )
    op.drop_index(
        op.f("ix_reconciliation_cases_account_id"),
        table_name="reconciliation_cases",
    )
    op.drop_index(
        op.f("ix_reconciliation_cases_user_id"),
        table_name="reconciliation_cases",
    )
    op.drop_table("reconciliation_cases")
