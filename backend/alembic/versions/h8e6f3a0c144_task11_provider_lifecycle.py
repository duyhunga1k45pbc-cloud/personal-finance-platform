"""task11 provider transaction lifecycle orchestration

Revision ID: h8e6f3a0c144
Revises: g7d5e2f9b033
Create Date: 2026-09-03
"""

from alembic import op
import datetime
import sqlalchemy as sa


revision = "h8e6f3a0c144"
down_revision = "g7d5e2f9b033"
branch_labels = None
depends_on = None


def _immutable_history_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_provider_transaction_lifecycle_history_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider transaction lifecycle history is append-only';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_provider_transaction_lifecycle_history_immutable
        BEFORE UPDATE OR DELETE ON provider_transaction_lifecycle_history
        FOR EACH ROW
        EXECUTE FUNCTION reject_provider_transaction_lifecycle_history_mutation();
    """))


def _drop_immutable_history_trigger() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_provider_transaction_lifecycle_history_immutable "
        "ON provider_transaction_lifecycle_history"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS reject_provider_transaction_lifecycle_history_mutation()"
    ))


def _iso_utc(value) -> str:
    if isinstance(value, str):
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    else:
        parsed = parsed.astimezone(datetime.timezone.utc)
    return parsed.isoformat()


def upgrade() -> None:
    op.create_table(
        "provider_transaction_lifecycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("external_transaction_record_id", sa.Integer(), nullable=False),
        sa.Column("current_status", sa.String(length=16), nullable=False),
        sa.Column("current_candidate_id", sa.Integer(), nullable=False),
        sa.Column("current_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posted_candidate_id", sa.Integer(), nullable=True),
        sa.Column("reversed_candidate_id", sa.Integer(), nullable=True),
        sa.Column("canonical_event_id", sa.Integer(), nullable=True),
        sa.Column("reversal_event_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "current_status IN ('PENDING', 'POSTED', 'REVERSED')",
            name="ck_provider_transaction_lifecycles_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_provider_transaction_lifecycles_version_positive",
        ),
        sa.CheckConstraint(
            "(current_status = 'PENDING' AND posted_candidate_id IS NULL AND reversed_candidate_id IS NULL AND canonical_event_id IS NULL AND reversal_event_id IS NULL) OR "
            "(current_status = 'POSTED' AND posted_candidate_id IS NOT NULL AND reversed_candidate_id IS NULL AND reversal_event_id IS NULL) OR "
            "(current_status = 'REVERSED' AND reversed_candidate_id IS NOT NULL AND ((canonical_event_id IS NULL AND reversal_event_id IS NULL) OR (canonical_event_id IS NOT NULL AND reversal_event_id IS NOT NULL)))",
            name="ck_provider_transaction_lifecycles_state_shape",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["external_transaction_record_id"], ["external_transactions.id"]),
        sa.ForeignKeyConstraint(["current_candidate_id"], ["provider_normalized_candidates.id"]),
        sa.ForeignKeyConstraint(["posted_candidate_id"], ["provider_normalized_candidates.id"]),
        sa.ForeignKeyConstraint(["reversed_candidate_id"], ["provider_normalized_candidates.id"]),
        sa.ForeignKeyConstraint(["canonical_event_id"], ["financial_events.id"]),
        sa.ForeignKeyConstraint(["reversal_event_id"], ["financial_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_transaction_record_id",
            name="uq_provider_transaction_lifecycles_external_transaction",
        ),
        sa.UniqueConstraint(
            "canonical_event_id",
            name="uq_provider_transaction_lifecycles_canonical_event",
        ),
        sa.UniqueConstraint(
            "reversal_event_id",
            name="uq_provider_transaction_lifecycles_reversal_event",
        ),
    )
    op.create_index(op.f("ix_provider_transaction_lifecycles_user_id"), "provider_transaction_lifecycles", ["user_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_external_transaction_record_id"), "provider_transaction_lifecycles", ["external_transaction_record_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_current_candidate_id"), "provider_transaction_lifecycles", ["current_candidate_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_posted_candidate_id"), "provider_transaction_lifecycles", ["posted_candidate_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_reversed_candidate_id"), "provider_transaction_lifecycles", ["reversed_candidate_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_canonical_event_id"), "provider_transaction_lifecycles", ["canonical_event_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycles_reversal_event_id"), "provider_transaction_lifecycles", ["reversal_event_id"], unique=False)

    op.create_table(
        "provider_transaction_lifecycle_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lifecycle_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lifecycle_version", sa.Integer(), nullable=False),
        sa.Column("transition_type", sa.String(length=32), nullable=False),
        sa.Column("source_candidate_id", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.JSON(), nullable=True),
        sa.Column("new_state", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "lifecycle_version >= 1",
            name="ck_provider_transaction_lifecycle_history_version_positive",
        ),
        sa.CheckConstraint(
            "transition_type IN ('INITIALIZED', 'ADVANCED', 'REFRESHED', 'CANONICAL_LINKED')",
            name="ck_provider_transaction_lifecycle_history_transition_type",
        ),
        sa.ForeignKeyConstraint(["lifecycle_id"], ["provider_transaction_lifecycles.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["provider_normalized_candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lifecycle_id",
            "lifecycle_version",
            name="uq_provider_transaction_lifecycle_history_version",
        ),
    )
    op.create_index(op.f("ix_provider_transaction_lifecycle_history_lifecycle_id"), "provider_transaction_lifecycle_history", ["lifecycle_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycle_history_user_id"), "provider_transaction_lifecycle_history", ["user_id"], unique=False)
    op.create_index(op.f("ix_provider_transaction_lifecycle_history_source_candidate_id"), "provider_transaction_lifecycle_history", ["source_candidate_id"], unique=False)

    _immutable_history_trigger()

    # Backfill only the current Task-10 interpretation candidate. Raw evidence and
    # normalized candidates remain unchanged. A pre-existing canonical event whose
    # current candidate is already REVERSED requires causal resolution, so fail
    # rather than silently inventing a financial reversal during migration.
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT
            i.user_id,
            i.external_transaction_record_id,
            i.canonical_event_id
        FROM provider_transaction_interpretations i
        ORDER BY i.id
    """)).mappings().all()

    for row in rows:
        # Task 10 intentionally kept USER_CONFIRMED interpretation pinned even when
        # newer immutable evidence arrived. Task 11 therefore backfills source
        # lifecycle from the newest non-UNKNOWN observation, not from the pinned
        # interpretation candidate.
        candidates = bind.execute(sa.text("""
            SELECT
                c.id AS candidate_id,
                c.normalized_status,
                e.observed_at
            FROM provider_normalized_candidates c
            JOIN external_transaction_evidence e ON e.id = c.source_evidence_id
            WHERE c.external_transaction_record_id = :transaction_id
              AND c.normalized_status <> 'UNKNOWN'
            ORDER BY e.observed_at ASC, c.id ASC
        """), {"transaction_id": row["external_transaction_record_id"]}).mappings().all()
        if not candidates:
            continue

        allowed = {
            "PENDING": {"PENDING", "POSTED", "REVERSED"},
            "POSTED": {"POSTED", "REVERSED"},
            "REVERSED": {"REVERSED"},
        }
        current = None
        current_time = None
        for candidate_row in candidates:
            candidate_status = candidate_row["normalized_status"]
            if candidate_status not in allowed:
                raise RuntimeError(f"Unsupported existing provider lifecycle status: {candidate_status}")
            candidate_time_raw = candidate_row["observed_at"]
            if isinstance(candidate_time_raw, str):
                candidate_time = datetime.datetime.fromisoformat(candidate_time_raw.replace("Z", "+00:00"))
            else:
                candidate_time = candidate_time_raw
            if candidate_time.tzinfo is None:
                candidate_time = candidate_time.replace(tzinfo=datetime.timezone.utc)
            else:
                candidate_time = candidate_time.astimezone(datetime.timezone.utc)

            if current is None:
                current = candidate_row
                current_time = candidate_time
                continue
            if candidate_time == current_time:
                if candidate_status != current["normalized_status"]:
                    raise RuntimeError(
                        "Cannot backfill conflicting provider lifecycle statuses for the same observed_at"
                    )
                current = candidate_row
                continue
            if candidate_status in allowed[current["normalized_status"]]:
                current = candidate_row
                current_time = candidate_time
            # Invalid regressions are preserved as normalized evidence but do not
            # regress the derived source lifecycle.

        latest = current
        status = latest["normalized_status"]
        candidate_id = latest["candidate_id"]
        canonical_event_id = row["canonical_event_id"]
        observed_at = latest["observed_at"]
        observed_at_json = _iso_utc(observed_at)
        if status == "PENDING" and canonical_event_id is not None:
            raise RuntimeError("Existing PENDING provider interpretation unexpectedly has a canonical event")
        if status == "REVERSED" and canonical_event_id is not None:
            raise RuntimeError(
                "Cannot auto-backfill a REVERSED provider transaction that already has a canonical event; "
                "reprocess that source transaction through Task 11 so a traced REVERSAL_OF event is created"
            )

        result = bind.execute(sa.text("""
            INSERT INTO provider_transaction_lifecycles (
                user_id,
                external_transaction_record_id,
                current_status,
                current_candidate_id,
                current_observed_at,
                posted_candidate_id,
                reversed_candidate_id,
                canonical_event_id,
                reversal_event_id,
                version
            ) VALUES (
                :user_id,
                :transaction_id,
                :status,
                :candidate_id,
                :observed_at,
                :posted_candidate_id,
                :reversed_candidate_id,
                :canonical_event_id,
                NULL,
                1
            )
            RETURNING id
        """), {
            "user_id": row["user_id"],
            "transaction_id": row["external_transaction_record_id"],
            "status": status,
            "candidate_id": candidate_id,
            "observed_at": observed_at,
            "posted_candidate_id": candidate_id if status == "POSTED" else None,
            "reversed_candidate_id": candidate_id if status == "REVERSED" else None,
            "canonical_event_id": canonical_event_id if status == "POSTED" else None,
        })
        lifecycle_id = result.scalar_one()
        snapshot = {
            "id": lifecycle_id,
            "user_id": row["user_id"],
            "external_transaction_record_id": row["external_transaction_record_id"],
            "current_status": status,
            "current_candidate_id": candidate_id,
            "current_observed_at": observed_at_json,
            "posted_candidate_id": candidate_id if status == "POSTED" else None,
            "reversed_candidate_id": candidate_id if status == "REVERSED" else None,
            "canonical_event_id": canonical_event_id if status == "POSTED" else None,
            "reversal_event_id": None,
            "version": 1,
        }
        history_insert = sa.text("""
            INSERT INTO provider_transaction_lifecycle_history (
                lifecycle_id,
                user_id,
                lifecycle_version,
                transition_type,
                source_candidate_id,
                previous_state,
                new_state
            ) VALUES (
                :lifecycle_id,
                :user_id,
                1,
                'INITIALIZED',
                :candidate_id,
                NULL,
                :new_state
            )
        """).bindparams(sa.bindparam("new_state", type_=sa.JSON()))
        bind.execute(history_insert, {
            "lifecycle_id": lifecycle_id,
            "user_id": row["user_id"],
            "candidate_id": candidate_id,
            "new_state": snapshot,
        })


def downgrade() -> None:
    bind = op.get_bind()
    lifecycle_count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_transaction_lifecycles")).scalar()
    history_count = bind.execute(sa.text("SELECT COUNT(*) FROM provider_transaction_lifecycle_history")).scalar()
    if lifecycle_count or history_count:
        raise RuntimeError(
            "Cannot downgrade Task 11 while provider lifecycle state/history exists; "
            "downgrade would discard source-state traceability."
        )

    _drop_immutable_history_trigger()
    op.drop_index(op.f("ix_provider_transaction_lifecycle_history_source_candidate_id"), table_name="provider_transaction_lifecycle_history")
    op.drop_index(op.f("ix_provider_transaction_lifecycle_history_user_id"), table_name="provider_transaction_lifecycle_history")
    op.drop_index(op.f("ix_provider_transaction_lifecycle_history_lifecycle_id"), table_name="provider_transaction_lifecycle_history")
    op.drop_table("provider_transaction_lifecycle_history")

    op.drop_index(op.f("ix_provider_transaction_lifecycles_reversal_event_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_canonical_event_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_reversed_candidate_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_posted_candidate_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_current_candidate_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_external_transaction_record_id"), table_name="provider_transaction_lifecycles")
    op.drop_index(op.f("ix_provider_transaction_lifecycles_user_id"), table_name="provider_transaction_lifecycles")
    op.drop_table("provider_transaction_lifecycles")
