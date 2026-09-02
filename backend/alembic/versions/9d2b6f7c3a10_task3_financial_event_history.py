"""task3 financial event history

Revision ID: 9d2b6f7c3a10
Revises: 5c1a7e4b2d90
Create Date: 2026-09-02
"""

from __future__ import annotations

from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision = "9d2b6f7c3a10"
down_revision = "5c1a7e4b2d90"
branch_labels = None
depends_on = None


def _time_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _snapshot(row, entries) -> dict:
    return {
        "event_type": row["event_type"],
        "description": row["description"],
        "category": row["category"],
        "occurred_at": _time_value(row["occurred_at"]),
        "effective_at": _time_value(row["effective_at"]),
        "interpretation_state": row["interpretation_state"],
        "provenance": row["provenance"],
        "confidence": row["confidence"],
        "lifecycle_state": "ACTIVE",
        "version": row["version"],
        "entries": [
            {
                "account_id": entry["account_id"],
                "amount": format(Decimal(str(entry["amount"])).quantize(Decimal("0.01")), "f"),
            }
            for entry in entries
        ],
    }


def _assert_task2_state_is_migratable() -> None:
    bind = op.get_bind()
    bad_versions = bind.execute(
        sa.text("SELECT COUNT(*) FROM financial_events WHERE version <> 1")
    ).scalar_one()
    if bad_versions:
        raise RuntimeError(
            "Task 3 migration refused: pre-history financial events must still be version 1"
        )

    bad_entry_counts = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM financial_events fe
            LEFT JOIN (
                SELECT financial_event_id, COUNT(*) AS entry_count
                FROM financial_event_entries
                GROUP BY financial_event_id
            ) x ON x.financial_event_id = fe.id
            WHERE COALESCE(x.entry_count, 0) <> 1
            """
        )
    ).scalar_one()
    if bad_entry_counts:
        raise RuntimeError(
            "Task 3 migration refused: Task 2 compatibility events must have exactly one entry"
        )


def _create_postgres_append_only_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_financial_event_history_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'financial_event_history is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_financial_event_history_append_only
            BEFORE UPDATE OR DELETE ON financial_event_history
            FOR EACH ROW
            EXECUTE FUNCTION reject_financial_event_history_mutation();
            """
        )
    )


def upgrade() -> None:
    _assert_task2_state_is_migratable()

    with op.batch_alter_table("financial_events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(),
                nullable=False,
                server_default="ACTIVE",
            )
        )
        batch_op.create_check_constraint(
            "ck_financial_events_lifecycle_state",
            "lifecycle_state IN ('ACTIVE', 'VOIDED')",
        )

    op.create_table(
        "financial_event_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_event_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
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
            "transition_type IN ('CREATED', 'CORRECTED', 'VOIDED')",
            name="ck_financial_event_history_transition_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'PROVIDER', 'AI')",
            name="ck_financial_event_history_actor_type",
        ),
        sa.CheckConstraint(
            "event_version >= 1",
            name="ck_financial_event_history_version_positive",
        ),
        sa.ForeignKeyConstraint(["financial_event_id"], ["financial_events.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "financial_event_id",
            "event_version",
            name="uq_financial_event_history_event_version",
        ),
    )
    op.create_index(
        op.f("ix_financial_event_history_id"),
        "financial_event_history",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_event_history_financial_event_id"),
        "financial_event_history",
        ["financial_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_financial_event_history_user_id"),
        "financial_event_history",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_financial_event_history_event_recorded",
        "financial_event_history",
        ["financial_event_id", "recorded_at"],
        unique=False,
    )

    bind = op.get_bind()
    history_table = sa.table(
        "financial_event_history",
        sa.column("financial_event_id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("event_version", sa.Integer()),
        sa.column("transition_type", sa.String()),
        sa.column("actor_type", sa.String()),
        sa.column("actor_user_id", sa.Integer()),
        sa.column("previous_state", sa.JSON()),
        sa.column("new_state", sa.JSON()),
        sa.column("reason", sa.String()),
        sa.column("recorded_at", sa.DateTime(timezone=True)),
    )

    events = bind.execute(
        sa.text(
            """
            SELECT id, user_id, event_type, description, category,
                   occurred_at, effective_at, interpretation_state,
                   provenance, confidence, version, recorded_at
            FROM financial_events
            ORDER BY id
            """
        )
    ).mappings().all()

    for row in events:
        entries = bind.execute(
            sa.text(
                """
                SELECT account_id, amount
                FROM financial_event_entries
                WHERE financial_event_id = :event_id
                ORDER BY id
                """
            ),
            {"event_id": row["id"]},
        ).mappings().all()

        bind.execute(
            history_table.insert().values(
                financial_event_id=row["id"],
                user_id=row["user_id"],
                event_version=1,
                transition_type="CREATED",
                actor_type="SYSTEM",
                actor_user_id=None,
                previous_state=None,
                new_state=_snapshot(row, entries),
                reason="task3_history_backfill",
            )
        )

    history_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM financial_event_history")
    ).scalar_one()
    event_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM financial_events")
    ).scalar_one()
    if history_count != event_count:
        raise RuntimeError(
            "Task 3 migration parity failed: every existing event needs one CREATED history row"
        )

    _create_postgres_append_only_trigger()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_financial_event_history_append_only ON financial_event_history"
            )
        )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS reject_financial_event_history_mutation()"
            )
        )

    op.drop_index(
        "ix_financial_event_history_event_recorded",
        table_name="financial_event_history",
    )
    op.drop_index(
        op.f("ix_financial_event_history_user_id"),
        table_name="financial_event_history",
    )
    op.drop_index(
        op.f("ix_financial_event_history_financial_event_id"),
        table_name="financial_event_history",
    )
    op.drop_index(
        op.f("ix_financial_event_history_id"),
        table_name="financial_event_history",
    )
    op.drop_table("financial_event_history")

    with op.batch_alter_table("financial_events") as batch_op:
        batch_op.drop_constraint(
            "ck_financial_events_lifecycle_state",
            type_="check",
        )
        batch_op.drop_column("lifecycle_state")
