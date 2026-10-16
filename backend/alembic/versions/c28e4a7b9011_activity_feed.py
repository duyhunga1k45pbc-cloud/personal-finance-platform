"""add durable user-scoped activity feed

Revision ID: c28e4a7b9011
Revises: b17ce17cd722
"""

import sqlalchemy as sa
from alembic import op

revision = "c28e4a7b9011"
down_revision = "b17ce17cd722"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_feed_cursors",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "last_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="ck_activity_feed_cursor_nonnegative",
        ),
    )

    op.create_table(
        "activity_feed_events",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "sequence",
            name="pk_activity_feed_events",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_activity_feed_sequence_positive",
        ),
    )


def downgrade() -> None:
    op.drop_table("activity_feed_events")
    op.drop_table("activity_feed_cursors")
