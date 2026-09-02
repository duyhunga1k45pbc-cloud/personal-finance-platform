"""task7 refunds reversals and causal links

Revision ID: d4a9b7c2e810
Revises: c3f8a2e1d605
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "d4a9b7c2e810"
down_revision = "c3f8a2e1d605"
branch_labels = None
depends_on = None


OLD_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER')"
)
NEW_COMMAND_TYPES = (
    "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', "
    "'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL')"
)


def _create_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_financial_event_link_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'financial_event_links is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_financial_event_links_append_only
            BEFORE UPDATE OR DELETE ON financial_event_links
            FOR EACH ROW
            EXECUTE FUNCTION reject_financial_event_link_mutation();
            """
        )
    )


def _drop_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_financial_event_links_append_only ON financial_event_links"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_financial_event_link_mutation()"))


def upgrade() -> None:
    op.create_table(
        "financial_event_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_event_id", sa.Integer(), nullable=False),
        sa.Column("to_event_id", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "relation_type IN ('REFUND_OF', 'REVERSAL_OF')",
            name="ck_financial_event_links_relation_type",
        ),
        sa.CheckConstraint(
            "from_event_id <> to_event_id",
            name="ck_financial_event_links_not_self",
        ),
        sa.ForeignKeyConstraint(["from_event_id"], ["financial_events.id"]),
        sa.ForeignKeyConstraint(["to_event_id"], ["financial_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_event_id",
            name="uq_financial_event_links_from_event",
        ),
    )
    op.create_index(
        op.f("ix_financial_event_links_from_event_id"),
        "financial_event_links",
        ["from_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_event_links_to_event_id"),
        "financial_event_links",
        ["to_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_financial_event_links_to_relation",
        "financial_event_links",
        ["to_event_id", "relation_type"],
        unique=False,
    )
    _create_postgres_append_only_trigger()

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                NEW_COMMAND_TYPES,
            )
    else:
        op.drop_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            NEW_COMMAND_TYPES,
        )


def downgrade() -> None:
    bind = op.get_bind()
    causal_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM financial_event_links "
            "WHERE relation_type IN ('REFUND_OF', 'REVERSAL_OF')"
        )
    ).scalar()
    receipt_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM command_receipts "
            "WHERE command_type IN ('CREATE_REFUND', 'CREATE_REVERSAL')"
        )
    ).scalar()
    if causal_count or receipt_count:
        raise RuntimeError(
            "Cannot downgrade Task 7 while refund/reversal causal state exists. "
            "Downgrade would discard economic causality."
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("command_receipts", recreate="always") as batch:
            batch.drop_constraint("ck_command_receipts_command_type", type_="check")
            batch.create_check_constraint(
                "ck_command_receipts_command_type",
                OLD_COMMAND_TYPES,
            )
    else:
        op.drop_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_command_receipts_command_type",
            "command_receipts",
            OLD_COMMAND_TYPES,
        )

    _drop_postgres_append_only_trigger()
    op.drop_index("ix_financial_event_links_to_relation", table_name="financial_event_links")
    op.drop_index(op.f("ix_financial_event_links_to_event_id"), table_name="financial_event_links")
    op.drop_index(op.f("ix_financial_event_links_from_event_id"), table_name="financial_event_links")
    op.drop_table("financial_event_links")
