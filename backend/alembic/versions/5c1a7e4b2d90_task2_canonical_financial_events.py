"""task2 canonical financial events

Revision ID: 5c1a7e4b2d90
Revises: 8f4c2d91a601
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "5c1a7e4b2d90"
down_revision: Union[str, Sequence[str], None] = "8f4c2d91a601"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_legacy_state_is_migratable() -> None:
    bind = op.get_bind()

    unsupported_type_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM transactions
            WHERE type NOT IN ('income', 'expense')
            """
        )
    ).scalar_one()
    if unsupported_type_count:
        raise RuntimeError(
            "Task 2 migration refused: legacy transactions contain unsupported types"
        )

    nonpositive_amount_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM transactions
            WHERE amount <= 0
            """
        )
    ).scalar_one()
    if nonpositive_amount_count:
        raise RuntimeError(
            "Task 2 migration refused: legacy transactions contain non-positive amounts"
        )

    missing_time_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM transactions
            WHERE date IS NULL
            """
        )
    ).scalar_one()
    if missing_time_count:
        raise RuntimeError(
            "Task 2 migration refused: legacy transactions contain missing occurrence time"
        )

    ownership_mismatch_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM transactions t
            JOIN financial_accounts fa ON fa.id = t.account_id
            WHERE t.user_id <> fa.user_id
            """
        )
    ).scalar_one()
    if ownership_mismatch_count:
        raise RuntimeError(
            "Task 2 migration refused: transaction/account ownership mismatch exists"
        )


def _assert_backfill_parity() -> None:
    bind = op.get_bind()

    legacy_count = bind.execute(sa.text("SELECT COUNT(*) FROM transactions")).scalar_one()
    event_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM financial_events
            WHERE legacy_transaction_id IS NOT NULL
            """
        )
    ).scalar_one()
    entry_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM financial_event_entries")
    ).scalar_one()

    if legacy_count != event_count or legacy_count != entry_count:
        raise RuntimeError(
            "Task 2 migration parity failed: legacy/event/entry counts differ"
        )

    mismatched_rows = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM transactions t
            JOIN financial_events fe
              ON fe.legacy_transaction_id = t.id
            JOIN financial_event_entries fee
              ON fee.financial_event_id = fe.id
            WHERE fe.user_id <> t.user_id
               OR fe.event_type <> UPPER(t.type)
               OR fee.account_id <> t.account_id
               OR fee.amount <> CASE
                    WHEN t.type = 'income' THEN t.amount
                    ELSE -t.amount
                  END
            """
        )
    ).scalar_one()
    if mismatched_rows:
        raise RuntimeError(
            "Task 2 migration parity failed: canonical rows diverge from legacy rows"
        )


def upgrade() -> None:
    _assert_legacy_state_is_migratable()

    op.create_table(
        "financial_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("legacy_transaction_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "interpretation_state",
            sa.String(),
            nullable=False,
            server_default="USER_CONFIRMED",
        ),
        sa.Column(
            "provenance",
            sa.String(),
            nullable=False,
            server_default="USER_MANUAL",
        ),
        sa.Column(
            "confidence",
            sa.String(),
            nullable=False,
            server_default="USER_CONFIRMED",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "event_type IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REFUND', 'REVERSAL', 'ADJUSTMENT')",
            name="ck_financial_events_event_type",
        ),
        sa.CheckConstraint(
            "interpretation_state IN ('UNCLASSIFIED', 'CLASSIFIED', 'USER_CONFIRMED')",
            name="ck_financial_events_interpretation_state",
        ),
        sa.CheckConstraint(
            "provenance IN ('PROVIDER', 'USER_MANUAL', 'SYSTEM_INFERRED')",
            name="ck_financial_events_provenance",
        ),
        sa.CheckConstraint(
            "confidence IN ('OBSERVED', 'INFERRED', 'USER_CONFIRMED')",
            name="ck_financial_events_confidence",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_financial_events_version_positive",
        ),
        sa.ForeignKeyConstraint(["legacy_transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "legacy_transaction_id",
            name="uq_financial_events_legacy_transaction_id",
        ),
    )
    op.create_index(
        op.f("ix_financial_events_id"),
        "financial_events",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_events_user_id"),
        "financial_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_events_legacy_transaction_id"),
        "financial_events",
        ["legacy_transaction_id"],
        unique=False,
    )

    op.create_table(
        "financial_event_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_event_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount <> 0",
            name="ck_financial_event_entries_nonzero_amount",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["financial_accounts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_financial_event_entries_id"),
        "financial_event_entries",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_event_entries_financial_event_id"),
        "financial_event_entries",
        ["financial_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_event_entries_account_id"),
        "financial_event_entries",
        ["account_id"],
        unique=False,
    )

    # Every current legacy transaction is user-authored and already classified.
    # Preserve the original occurrence timestamp exactly; do not infer timezone.
    op.execute(
        sa.text(
            """
            INSERT INTO financial_events (
                user_id,
                legacy_transaction_id,
                event_type,
                description,
                category,
                occurred_at,
                effective_at,
                interpretation_state,
                provenance,
                confidence,
                version
            )
            SELECT
                user_id,
                id,
                UPPER(type),
                description,
                category,
                date,
                date,
                'USER_CONFIRMED',
                'USER_MANUAL',
                'USER_CONFIRMED',
                1
            FROM transactions
            ORDER BY id
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO financial_event_entries (
                financial_event_id,
                account_id,
                amount
            )
            SELECT
                fe.id,
                t.account_id,
                CASE
                    WHEN t.type = 'income' THEN t.amount
                    ELSE -t.amount
                END
            FROM transactions t
            JOIN financial_events fe
              ON fe.legacy_transaction_id = t.id
            ORDER BY t.id
            """
        )
    )

    _assert_backfill_parity()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_financial_event_entries_account_id"),
        table_name="financial_event_entries",
    )
    op.drop_index(
        op.f("ix_financial_event_entries_financial_event_id"),
        table_name="financial_event_entries",
    )
    op.drop_index(
        op.f("ix_financial_event_entries_id"),
        table_name="financial_event_entries",
    )
    op.drop_table("financial_event_entries")

    op.drop_index(
        op.f("ix_financial_events_legacy_transaction_id"),
        table_name="financial_events",
    )
    op.drop_index(
        op.f("ix_financial_events_user_id"),
        table_name="financial_events",
    )
    op.drop_index(
        op.f("ix_financial_events_id"),
        table_name="financial_events",
    )
    op.drop_table("financial_events")
