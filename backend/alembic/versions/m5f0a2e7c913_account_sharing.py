"""add viewer grants for financial accounts

Revision ID: m5f0a2e7c913
Revises: c28e4a7b9011
"""

from alembic import op
import sqlalchemy as sa


revision = "m5f0a2e7c913"
down_revision = "c28e4a7b9011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("grantee_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.String(length=16),
            server_default="VIEWER",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role = 'VIEWER'",
            name="ck_account_grants_role_viewer",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["financial_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["grantee_user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "grantee_user_id",
            name="uq_account_grants_account_grantee",
        ),
    )
    op.create_index(
        "ix_account_grants_grantee_account",
        "account_grants",
        ["grantee_user_id", "account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_grants_grantee_account",
        table_name="account_grants",
    )
    op.drop_table("account_grants")
