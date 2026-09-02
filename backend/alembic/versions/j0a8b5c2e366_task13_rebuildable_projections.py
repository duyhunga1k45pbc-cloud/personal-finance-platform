"""task13 rebuildable financial projections

Revision ID: j0a8b5c2e366
Revises: i9f7a4b1d255
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "j0a8b5c2e366"
down_revision = "i9f7a4b1d255"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_projection_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("canonical_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("total_income", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_expense", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("economic_balance", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("net_worth", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("account_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "rebuilt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_financial_projection_state_generation_positive",
        ),
        sa.CheckConstraint(
            "length(canonical_fingerprint) = 64",
            name="ck_financial_projection_state_fingerprint_length",
        ),
        sa.CheckConstraint(
            "account_count >= 0",
            name="ck_financial_projection_state_account_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_financial_projection_state_user"),
    )
    op.create_index(
        op.f("ix_financial_projection_state_user_id"),
        "financial_projection_state",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "financial_account_balance_projections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column(
            "rebuilt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_financial_account_balance_projections_generation_positive",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["financial_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "account_id",
            name="uq_financial_account_balance_projections_user_account",
        ),
    )
    op.create_index(
        op.f("ix_financial_account_balance_projections_user_id"),
        "financial_account_balance_projections",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_account_balance_projections_account_id"),
        "financial_account_balance_projections",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_financial_account_balance_projections_user_generation",
        "financial_account_balance_projections",
        ["user_id", "generation"],
        unique=False,
    )


def downgrade() -> None:
    # Projections are explicitly disposable. Unlike raw evidence/history, they
    # may be dropped without information loss because canonical state remains.
    op.drop_index(
        "ix_financial_account_balance_projections_user_generation",
        table_name="financial_account_balance_projections",
    )
    op.drop_index(
        op.f("ix_financial_account_balance_projections_account_id"),
        table_name="financial_account_balance_projections",
    )
    op.drop_index(
        op.f("ix_financial_account_balance_projections_user_id"),
        table_name="financial_account_balance_projections",
    )
    op.drop_table("financial_account_balance_projections")
    op.drop_index(
        op.f("ix_financial_projection_state_user_id"),
        table_name="financial_projection_state",
    )
    op.drop_table("financial_projection_state")
