"""task5 internal transfers

Revision ID: c3f8a2e1d605
Revises: b7e4c1d9a402
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "c3f8a2e1d605"
down_revision = "b7e4c1d9a402"
branch_labels = None
depends_on = None


OLD_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', 'DELETE_TRANSACTION')"
)
NEW_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER')"
)
ONE_TARGET = (
    "(transaction_id IS NOT NULL AND financial_event_id IS NULL) OR "
    "(transaction_id IS NULL AND financial_event_id IS NOT NULL)"
)


def _has_transfer_receipts(bind) -> bool:
    value = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM command_receipts "
            "WHERE command_type = 'CREATE_TRANSFER'"
        )
    ).scalar()
    return bool(value)


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint(
                "ck_command_receipts_command_type",
                type_="check",
            )
            batch.alter_column(
                "transaction_id",
                existing_type=sa.Integer(),
                nullable=True,
            )
            batch.add_column(
                sa.Column("financial_event_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_command_receipts_financial_event_id",
                "financial_events",
                ["financial_event_id"],
                ["id"],
            )
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                NEW_COMMAND_TYPES,
            )
            batch.create_check_constraint(
                "ck_command_receipts_exactly_one_target",
                ONE_TARGET,
            )
        op.create_index(
            op.f("ix_command_receipts_financial_event_id"),
            "command_receipts",
            ["financial_event_id"],
            unique=False,
        )
        return

    op.drop_constraint(
        "ck_command_receipts_command_type",
        "command_receipts",
        type_="check",
    )
    op.alter_column(
        "command_receipts",
        "transaction_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "command_receipts",
        sa.Column("financial_event_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_command_receipts_financial_event_id",
        "command_receipts",
        "financial_events",
        ["financial_event_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_command_receipts_financial_event_id"),
        "command_receipts",
        ["financial_event_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_command_receipts_command_type",
        "command_receipts",
        NEW_COMMAND_TYPES,
    )
    op.create_check_constraint(
        "ck_command_receipts_exactly_one_target",
        "command_receipts",
        ONE_TARGET,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_transfer_receipts(bind):
        raise RuntimeError(
            "Cannot downgrade Task 5 while CREATE_TRANSFER command receipts exist. "
            "Downgrade would discard their resource linkage."
        )

    if bind.dialect.name == "sqlite":
        op.drop_index(
            op.f("ix_command_receipts_financial_event_id"),
            table_name="command_receipts",
        )
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint(
                "ck_command_receipts_exactly_one_target",
                type_="check",
            )
            batch.drop_constraint(
                "ck_command_receipts_command_type",
                type_="check",
            )
            batch.drop_constraint(
                "fk_command_receipts_financial_event_id",
                type_="foreignkey",
            )
            batch.drop_column("financial_event_id")
            batch.alter_column(
                "transaction_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                OLD_COMMAND_TYPES,
            )
        return

    op.drop_constraint(
        "ck_command_receipts_exactly_one_target",
        "command_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_command_receipts_command_type",
        "command_receipts",
        type_="check",
    )
    op.drop_index(
        op.f("ix_command_receipts_financial_event_id"),
        table_name="command_receipts",
    )
    op.drop_constraint(
        "fk_command_receipts_financial_event_id",
        "command_receipts",
        type_="foreignkey",
    )
    op.drop_column("command_receipts", "financial_event_id")
    op.alter_column(
        "command_receipts",
        "transaction_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_command_receipts_command_type",
        "command_receipts",
        OLD_COMMAND_TYPES,
    )
