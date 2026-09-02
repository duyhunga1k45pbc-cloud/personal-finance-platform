"""task1 financial accounts vnd numeric

Revision ID: 8f4c2d91a601
Revises: 454649617ef6
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8f4c2d91a601"
down_revision: Union[str, Sequence[str], None] = "454649617ef6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _alter_amount_to_numeric() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "transactions",
            "amount",
            existing_type=sa.Float(),
            type_=sa.Numeric(18, 2),
            existing_nullable=False,
            postgresql_using="amount::numeric(18,2)",
        )
    else:
        with op.batch_alter_table("transactions") as batch_op:
            batch_op.alter_column(
                "amount",
                existing_type=sa.Float(),
                type_=sa.Numeric(18, 2),
                existing_nullable=False,
            )


def _alter_amount_to_float() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "transactions",
            "amount",
            existing_type=sa.Numeric(18, 2),
            type_=sa.Float(),
            existing_nullable=False,
            postgresql_using="amount::double precision",
        )
    else:
        with op.batch_alter_table("transactions") as batch_op:
            batch_op.alter_column(
                "amount",
                existing_type=sa.Numeric(18, 2),
                type_=sa.Float(),
                existing_nullable=False,
            )


def upgrade() -> None:
    op.create_table(
        "financial_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("account_type", sa.String(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("account_type IN ('BANK', 'EWALLET', 'CREDIT_CARD', 'CASH')", name="ck_financial_accounts_account_type"),
        sa.CheckConstraint("currency = 'VND'", name="ck_financial_accounts_currency_vnd"),
        sa.CheckConstraint("version >= 1", name="ck_financial_accounts_version_positive"),
        sa.CheckConstraint("NOT is_default OR account_type = 'CASH'", name="ck_financial_accounts_default_is_cash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_financial_accounts_id"), "financial_accounts", ["id"], unique=False)
    op.create_index(op.f("ix_financial_accounts_user_id"), "financial_accounts", ["user_id"], unique=False)
    op.create_index(
        "uq_financial_accounts_default_per_user",
        "financial_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )

    op.execute(sa.text("""
        INSERT INTO financial_accounts
            (user_id, name, account_type, currency, is_default, version)
        SELECT id, 'Default Cash', 'CASH', 'VND', TRUE, 1
        FROM users
    """))

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("transactions") as batch_op:
            batch_op.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_transactions_account_id_financial_accounts",
                "financial_accounts",
                ["account_id"],
                ["id"],
            )
            batch_op.create_index(op.f("ix_transactions_account_id"), ["account_id"], unique=False)
    else:
        op.add_column("transactions", sa.Column("account_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_transactions_account_id_financial_accounts",
            "transactions",
            "financial_accounts",
            ["account_id"],
            ["id"],
        )
        op.create_index(op.f("ix_transactions_account_id"), "transactions", ["account_id"], unique=False)

    op.execute(sa.text("""
        UPDATE transactions
        SET account_id = (
            SELECT financial_accounts.id
            FROM financial_accounts
            WHERE financial_accounts.user_id = transactions.user_id
              AND financial_accounts.is_default = TRUE
            LIMIT 1
        )
    """))

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.Integer(), nullable=False)

    _alter_amount_to_numeric()


def downgrade() -> None:
    _alter_amount_to_float()

    op.drop_index(op.f("ix_transactions_account_id"), table_name="transactions")
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("fk_transactions_account_id_financial_accounts", type_="foreignkey")
        batch_op.drop_column("account_id")

    op.drop_index("uq_financial_accounts_default_per_user", table_name="financial_accounts")
    op.drop_index(op.f("ix_financial_accounts_user_id"), table_name="financial_accounts")
    op.drop_index(op.f("ix_financial_accounts_id"), table_name="financial_accounts")
    op.drop_table("financial_accounts")
