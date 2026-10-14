"""add external identities

Revision ID: b17ce17cd722
Revises: l2c0d7e4a588
Create Date: 2026-10-14 19:10:20.765892

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b17ce17cd722'
down_revision: Union[str, Sequence[str], None] = 'l2c0d7e4a588'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "issuer",
            "subject",
            name="uq_external_identities_issuer_subject",
        ),
    )

    op.create_index(
        "ix_external_identities_user_id",
        "external_identities",
        ["user_id"],
    )


def downgrade():
    op.drop_index(
        "ix_external_identities_user_id",
        table_name="external_identities",
    )
    op.drop_table("external_identities")