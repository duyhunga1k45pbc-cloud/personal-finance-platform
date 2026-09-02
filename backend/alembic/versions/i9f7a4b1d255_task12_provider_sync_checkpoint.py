"""task12 provider sync checkpoint and failure recovery

Revision ID: i9f7a4b1d255
Revises: h8e6f3a0c144
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "i9f7a4b1d255"
down_revision = "h8e6f3a0c144"
branch_labels = None
depends_on = None


def _immutable_sync_trace_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_provider_sync_page_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider sync pages are append-only';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_provider_sync_pages_immutable
        BEFORE UPDATE OR DELETE ON provider_sync_pages
        FOR EACH ROW
        EXECUTE FUNCTION reject_provider_sync_page_mutation();
    """))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_provider_sync_page_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider sync page evidence trace is append-only';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_provider_sync_page_evidence_immutable
        BEFORE UPDATE OR DELETE ON provider_sync_page_evidence
        FOR EACH ROW
        EXECUTE FUNCTION reject_provider_sync_page_evidence_mutation();
    """))


def _drop_immutable_sync_trace_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_provider_sync_page_evidence_immutable "
        "ON provider_sync_page_evidence"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS reject_provider_sync_page_evidence_mutation()"
    ))
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_provider_sync_pages_immutable ON provider_sync_pages"
    ))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_provider_sync_page_mutation()"))


def upgrade() -> None:
    op.create_table(
        "provider_sync_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("committed_cursor", sa.String(length=1024), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_provider_sync_checkpoints_version_positive",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_connection_id",
            name="uq_provider_sync_checkpoints_connection",
        ),
    )
    op.create_index(
        op.f("ix_provider_sync_checkpoints_user_id"),
        "provider_sync_checkpoints",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_sync_checkpoints_provider_connection_id"),
        "provider_sync_checkpoints",
        ["provider_connection_id"],
        unique=False,
    )

    # Every provider connection gets a durable checkpoint before any worker can
    # commit a page. This gives page commits a stable row to lock and makes the
    # evidence+cursor transaction atomic even on the first sync.
    bind = op.get_bind()
    bind.execute(sa.text("""
        INSERT INTO provider_sync_checkpoints (
            user_id, provider_connection_id, committed_cursor, version
        )
        SELECT user_id, id, NULL, 1
        FROM provider_connections
    """))

    op.create_table(
        "provider_sync_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=False),
        sa.Column("request_cursor", sa.String(length=1024), nullable=True),
        sa.Column("request_cursor_key", sa.String(length=1024), nullable=False),
        sa.Column("next_cursor", sa.String(length=1024), nullable=True),
        sa.Column("has_more", sa.Boolean(), nullable=False),
        sa.Column("page_hash", sa.String(length=64), nullable=False),
        sa.Column("observations_count", sa.Integer(), nullable=False),
        sa.Column("evidence_created", sa.Integer(), nullable=False),
        sa.Column("evidence_deduplicated", sa.Integer(), nullable=False),
        sa.Column("checkpoint_version_before", sa.Integer(), nullable=False),
        sa.Column("checkpoint_version_after", sa.Integer(), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(page_hash) = 64",
            name="ck_provider_sync_pages_hash_length",
        ),
        sa.CheckConstraint(
            "observations_count >= 0 AND evidence_created >= 0 AND evidence_deduplicated >= 0",
            name="ck_provider_sync_pages_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "observations_count = evidence_created + evidence_deduplicated",
            name="ck_provider_sync_pages_counts_balance",
        ),
        sa.CheckConstraint(
            "checkpoint_version_before >= 1 AND checkpoint_version_after = checkpoint_version_before + 1",
            name="ck_provider_sync_pages_checkpoint_version_step",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_connection_id",
            "page_hash",
            name="uq_provider_sync_pages_connection_hash",
        ),
        sa.UniqueConstraint(
            "provider_connection_id",
            "checkpoint_version_after",
            name="uq_provider_sync_pages_connection_checkpoint_version",
        ),
    )
    op.create_index(
        op.f("ix_provider_sync_pages_user_id"),
        "provider_sync_pages",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_sync_pages_provider_connection_id"),
        "provider_sync_pages",
        ["provider_connection_id"],
        unique=False,
    )

    op.create_table(
        "provider_sync_page_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_page_id", sa.Integer(), nullable=False),
        sa.Column("external_evidence_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_evidence", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_provider_sync_page_evidence_ordinal_nonnegative",
        ),
        sa.ForeignKeyConstraint(["sync_page_id"], ["provider_sync_pages.id"]),
        sa.ForeignKeyConstraint(
            ["external_evidence_id"],
            ["external_transaction_evidence.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sync_page_id",
            "ordinal",
            name="uq_provider_sync_page_evidence_page_ordinal",
        ),
    )
    op.create_index(
        op.f("ix_provider_sync_page_evidence_sync_page_id"),
        "provider_sync_page_evidence",
        ["sync_page_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_sync_page_evidence_external_evidence_id"),
        "provider_sync_page_evidence",
        ["external_evidence_id"],
        unique=False,
    )

    _immutable_sync_trace_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    trace_count = bind.execute(sa.text(
        "SELECT (SELECT COUNT(*) FROM provider_sync_pages) + "
        "(SELECT COUNT(*) FROM provider_sync_page_evidence) + "
        "(SELECT COUNT(*) FROM provider_sync_checkpoints)"
    )).scalar()
    if trace_count:
        raise RuntimeError(
            "Cannot downgrade Task 12 while provider sync checkpoint/trace state exists; "
            "downgrade would discard failure-recovery evidence."
        )

    _drop_immutable_sync_trace_triggers()
    op.drop_index(
        op.f("ix_provider_sync_page_evidence_external_evidence_id"),
        table_name="provider_sync_page_evidence",
    )
    op.drop_index(
        op.f("ix_provider_sync_page_evidence_sync_page_id"),
        table_name="provider_sync_page_evidence",
    )
    op.drop_table("provider_sync_page_evidence")

    op.drop_index(
        op.f("ix_provider_sync_pages_provider_connection_id"),
        table_name="provider_sync_pages",
    )
    op.drop_index(
        op.f("ix_provider_sync_pages_user_id"),
        table_name="provider_sync_pages",
    )
    op.drop_table("provider_sync_pages")

    op.drop_index(
        op.f("ix_provider_sync_checkpoints_provider_connection_id"),
        table_name="provider_sync_checkpoints",
    )
    op.drop_index(
        op.f("ix_provider_sync_checkpoints_user_id"),
        table_name="provider_sync_checkpoints",
    )
    op.drop_table("provider_sync_checkpoints")
