"""task4 command idempotency

Revision ID: b7e4c1d9a402
Revises: 9d2b6f7c3a10
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa


revision = "b7e4c1d9a402"
down_revision = "9d2b6f7c3a10"
branch_labels = None
depends_on = None


def _create_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_command_receipt_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'command_receipts is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_command_receipts_append_only
            BEFORE UPDATE OR DELETE ON command_receipts
            FOR EACH ROW
            EXECUTE FUNCTION reject_command_receipt_mutation();
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("command_type", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', 'DELETE_TRANSACTION')",
            name="ck_command_receipts_command_type",
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "command_type",
            "idempotency_key",
            name="uq_command_receipts_user_command_key",
        ),
    )
    op.create_index(
        op.f("ix_command_receipts_id"),
        "command_receipts",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_command_receipts_user_id"),
        "command_receipts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_command_receipts_transaction_id"),
        "command_receipts",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_command_receipts_user_created",
        "command_receipts",
        ["user_id", "created_at"],
        unique=False,
    )

    _create_postgres_append_only_trigger()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_command_receipts_append_only ON command_receipts"
            )
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS reject_command_receipt_mutation()")
        )

    op.drop_index(
        "ix_command_receipts_user_created",
        table_name="command_receipts",
    )
    op.drop_index(
        op.f("ix_command_receipts_transaction_id"),
        table_name="command_receipts",
    )
    op.drop_index(
        op.f("ix_command_receipts_user_id"),
        table_name="command_receipts",
    )
    op.drop_index(
        op.f("ix_command_receipts_id"),
        table_name="command_receipts",
    )
    op.drop_table("command_receipts")
