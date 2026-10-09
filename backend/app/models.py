import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    text,
)

from app.database import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Numeric(18, 2), nullable=False)
    description = Column(String)
    category = Column(String)
    date = Column(DateTime, default=datetime.datetime.utcnow)
    type = Column(String, nullable=False, default="expense")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FinancialAccount(Base):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('BANK', 'EWALLET', 'CREDIT_CARD', 'CASH')",
            name="ck_financial_accounts_account_type",
        ),
        CheckConstraint(
            "currency = 'VND'",
            name="ck_financial_accounts_currency_vnd",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_financial_accounts_version_positive",
        ),
        CheckConstraint(
            "NOT is_default OR account_type = 'CASH'",
            name="ck_financial_accounts_default_is_cash",
        ),
        Index(
            "uq_financial_accounts_default_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    account_type = Column(String, nullable=False)
    currency = Column(String(3), nullable=False, default="VND")
    is_default = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class FinancialEvent(Base):
    __tablename__ = "financial_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REFUND', 'REVERSAL', 'ADJUSTMENT')",
            name="ck_financial_events_event_type",
        ),
        CheckConstraint(
            "interpretation_state IN ('UNCLASSIFIED', 'CLASSIFIED', 'USER_CONFIRMED')",
            name="ck_financial_events_interpretation_state",
        ),
        CheckConstraint(
            "provenance IN ('PROVIDER', 'USER_MANUAL', 'SYSTEM_INFERRED')",
            name="ck_financial_events_provenance",
        ),
        CheckConstraint(
            "confidence IN ('OBSERVED', 'INFERRED', 'USER_CONFIRMED')",
            name="ck_financial_events_confidence",
        ),
        CheckConstraint(
            "lifecycle_state IN ('ACTIVE', 'VOIDED')",
            name="ck_financial_events_lifecycle_state",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_financial_events_version_positive",
        ),
        UniqueConstraint(
            "legacy_transaction_id",
            name="uq_financial_events_legacy_transaction_id",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Transitional bridge while the legacy Transaction API remains available.
    legacy_transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=True,
        index=True,
    )

    event_type = Column(String, nullable=False)
    description = Column(String, nullable=True)
    category = Column(String, nullable=True)

    # Legacy Transaction.date has no timezone semantics. Preserve it exactly.
    occurred_at = Column(DateTime, nullable=False)
    effective_at = Column(DateTime, nullable=False)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    interpretation_state = Column(
        String,
        nullable=False,
        default="USER_CONFIRMED",
    )
    provenance = Column(String, nullable=False, default="USER_MANUAL")
    confidence = Column(String, nullable=False, default="USER_CONFIRMED")
    lifecycle_state = Column(String, nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)


class FinancialEventEntry(Base):
    __tablename__ = "financial_event_entries"
    __table_args__ = (
        CheckConstraint(
            "amount <> 0",
            name="ck_financial_event_entries_nonzero_amount",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    financial_event_id = Column(
        Integer,
        ForeignKey("financial_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )
    # Signed canonical movement for the account: + inflow, - outflow.
    amount = Column(Numeric(18, 2), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class FinancialEventLink(Base):
    __tablename__ = "financial_event_links"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('REFUND_OF', 'REVERSAL_OF')",
            name="ck_financial_event_links_relation_type",
        ),
        CheckConstraint(
            "from_event_id <> to_event_id",
            name="ck_financial_event_links_not_self",
        ),
        UniqueConstraint(
            "from_event_id",
            name="uq_financial_event_links_from_event",
        ),
        Index(
            "ix_financial_event_links_to_relation",
            "to_event_id",
            "relation_type",
        ),
    )

    id = Column(Integer, primary_key=True)
    from_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=False,
        index=True,
    )
    to_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=False,
        index=True,
    )
    relation_type = Column(String, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class FinancialEventHistory(Base):
    __tablename__ = "financial_event_history"
    __table_args__ = (
        CheckConstraint(
            "transition_type IN ('CREATED', 'CORRECTED', 'VOIDED')",
            name="ck_financial_event_history_transition_type",
        ),
        CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'PROVIDER', 'AI')",
            name="ck_financial_event_history_actor_type",
        ),
        CheckConstraint(
            "event_version >= 1",
            name="ck_financial_event_history_version_positive",
        ),
        UniqueConstraint(
            "financial_event_id",
            "event_version",
            name="uq_financial_event_history_event_version",
        ),
        Index(
            "ix_financial_event_history_event_recorded",
            "financial_event_id",
            "recorded_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    financial_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_version = Column(Integer, nullable=False)
    transition_type = Column(String, nullable=False)
    actor_type = Column(String, nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=False)
    reason = Column(String, nullable=True)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

class ReconciliationCase(Base):
    __tablename__ = "reconciliation_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UNKNOWN', 'RECONCILED', 'MISMATCH', 'RESOLVED')",
            name="ck_reconciliation_cases_status",
        ),
        CheckConstraint(
            "resolution_type IS NULL OR resolution_type IN ('REAL_EVENT', 'ADJUSTMENT')",
            name="ck_reconciliation_cases_resolution_type",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_reconciliation_cases_version_positive",
        ),
        CheckConstraint(
            "difference = observed_balance - expected_balance",
            name="ck_reconciliation_cases_difference_exact",
        ),
        CheckConstraint(
            "status <> 'RECONCILED' OR difference = 0",
            name="ck_reconciliation_cases_reconciled_zero_difference",
        ),
        CheckConstraint(
            "status <> 'MISMATCH' OR difference <> 0",
            name="ck_reconciliation_cases_mismatch_nonzero_difference",
        ),
        CheckConstraint(
            "(status = 'RESOLVED' AND resolution_type IS NOT NULL AND resolved_balance IS NOT NULL) OR "
            "(status <> 'RESOLVED' AND resolution_type IS NULL AND resolved_balance IS NULL)",
            name="ck_reconciliation_cases_resolution_state",
        ),
        CheckConstraint(
            "(resolution_type = 'ADJUSTMENT' AND adjustment_event_id IS NOT NULL) OR "
            "(COALESCE(resolution_type, '') <> 'ADJUSTMENT' AND adjustment_event_id IS NULL)",
            name="ck_reconciliation_cases_adjustment_target",
        ),
        UniqueConstraint(
            "adjustment_event_id",
            name="uq_reconciliation_cases_adjustment_event_id",
        ),
        Index(
            "ix_reconciliation_cases_user_status",
            "user_id",
            "status",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )
    expected_balance = Column(Numeric(18, 2), nullable=False)
    observed_balance = Column(Numeric(18, 2), nullable=False)
    difference = Column(Numeric(18, 2), nullable=False)
    status = Column(String, nullable=False)
    resolution_type = Column(String, nullable=True)
    adjustment_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=True,
        index=True,
    )
    resolved_balance = Column(Numeric(18, 2), nullable=True)
    note = Column(String, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    observed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ReconciliationHistory(Base):
    __tablename__ = "reconciliation_history"
    __table_args__ = (
        CheckConstraint(
            "transition_type IN ('DETECTED', 'RESOLVED_REAL_EVENT', 'RESOLVED_ADJUSTMENT')",
            name="ck_reconciliation_history_transition_type",
        ),
        CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'PROVIDER', 'AI')",
            name="ck_reconciliation_history_actor_type",
        ),
        CheckConstraint(
            "reconciliation_version >= 1",
            name="ck_reconciliation_history_version_positive",
        ),
        UniqueConstraint(
            "reconciliation_id",
            "reconciliation_version",
            name="uq_reconciliation_history_case_version",
        ),
        Index(
            "ix_reconciliation_history_case_recorded",
            "reconciliation_id",
            "recorded_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    reconciliation_id = Column(
        Integer,
        ForeignKey("reconciliation_cases.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    reconciliation_version = Column(Integer, nullable=False)
    transition_type = Column(String, nullable=False)
    actor_type = Column(String, nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=False)
    reason = Column(String, nullable=True)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider_name",
            "external_account_id",
            name="uq_provider_connections_user_provider_account",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(100), nullable=False)
    external_account_id = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ExternalTransaction(Base):
    __tablename__ = "external_transactions"
    __table_args__ = (
        UniqueConstraint(
            "provider_connection_id",
            "external_transaction_id",
            name="uq_external_transactions_connection_external_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_connection_id = Column(
        Integer,
        ForeignKey("provider_connections.id"),
        nullable=False,
        index=True,
    )
    external_transaction_id = Column(String(255), nullable=False)
    first_observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ExternalTransactionEvidence(Base):
    __tablename__ = "external_transaction_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_external_evidence_sha256_length",
        ),
        UniqueConstraint(
            "external_transaction_record_id",
            "payload_sha256",
            "observed_at",
            name="uq_external_evidence_exact_observation",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_connection_id = Column(
        Integer,
        ForeignKey("provider_connections.id"),
        nullable=False,
        index=True,
    )
    external_transaction_record_id = Column(
        Integer,
        ForeignKey("external_transactions.id"),
        nullable=False,
        index=True,
    )
    observed_at = Column(DateTime(timezone=True), nullable=False)
    raw_payload = Column(JSON, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderNormalizedCandidate(Base):
    __tablename__ = "provider_normalized_candidates"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_provider_normalized_candidates_amount_positive"),
        CheckConstraint("direction IN ('INFLOW', 'OUTFLOW')", name="ck_provider_normalized_candidates_direction"),
        CheckConstraint("normalized_status IN ('PENDING', 'POSTED', 'REVERSED', 'UNKNOWN')", name="ck_provider_normalized_candidates_status"),
        UniqueConstraint("source_evidence_id", "normalizer_version", name="uq_provider_normalized_candidate_evidence_version"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_connection_id = Column(Integer, ForeignKey("provider_connections.id"), nullable=False, index=True)
    external_transaction_record_id = Column(Integer, ForeignKey("external_transactions.id"), nullable=False, index=True)
    source_evidence_id = Column(Integer, ForeignKey("external_transaction_evidence.id"), nullable=False, index=True)
    normalizer_version = Column(String(64), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    direction = Column(String(16), nullable=False)
    normalized_status = Column(String(16), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    description = Column(String(255), nullable=True)
    provider_status = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ProviderTransactionInterpretation(Base):
    __tablename__ = "provider_transaction_interpretations"
    __table_args__ = (
        CheckConstraint("state IN ('UNCLASSIFIED', 'CLASSIFIED', 'USER_CONFIRMED')", name="ck_provider_interpretations_state"),
        CheckConstraint("event_type IS NULL OR event_type IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REFUND', 'REVERSAL')", name="ck_provider_interpretations_event_type"),
        CheckConstraint("confidence IS NULL OR confidence IN ('INFERRED', 'USER_CONFIRMED')", name="ck_provider_interpretations_confidence"),
        CheckConstraint("version >= 1", name="ck_provider_interpretations_version_positive"),
        CheckConstraint(
            "(state = 'UNCLASSIFIED' AND event_type IS NULL AND account_id IS NULL AND confidence IS NULL AND canonical_event_id IS NULL) OR "
            "(state = 'CLASSIFIED' AND event_type IS NOT NULL AND account_id IS NULL AND confidence = 'INFERRED' AND canonical_event_id IS NULL) OR "
            "(state = 'USER_CONFIRMED' AND event_type IS NOT NULL AND account_id IS NOT NULL AND confidence = 'USER_CONFIRMED')",
            name="ck_provider_interpretations_state_shape",
        ),
        CheckConstraint(
            "canonical_event_id IS NULL OR (state = 'USER_CONFIRMED' AND event_type IN ('INCOME', 'EXPENSE'))",
            name="ck_provider_interpretations_canonical_materialization_scope",
        ),
        UniqueConstraint("external_transaction_record_id", name="uq_provider_interpretations_external_transaction"),
        UniqueConstraint("canonical_event_id", name="uq_provider_interpretations_canonical_event"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    external_transaction_record_id = Column(Integer, ForeignKey("external_transactions.id"), nullable=False)
    normalized_candidate_id = Column(Integer, ForeignKey("provider_normalized_candidates.id"), nullable=False, index=True)
    state = Column(String(32), nullable=False)
    event_type = Column(String(32), nullable=True)
    account_id = Column(Integer, ForeignKey("financial_accounts.id"), nullable=True, index=True)
    canonical_event_id = Column(Integer, ForeignKey("financial_events.id"), nullable=True)
    confidence = Column(String(32), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ProviderInterpretationHistory(Base):
    __tablename__ = "provider_interpretation_history"
    __table_args__ = (
        CheckConstraint("interpretation_version >= 1", name="ck_provider_interpretation_history_version_positive"),
        CheckConstraint("transition_type IN ('NORMALIZED', 'CLASSIFIED', 'USER_CONFIRMED', 'MATERIALIZED')", name="ck_provider_interpretation_history_transition_type"),
        CheckConstraint("actor_type IN ('SYSTEM', 'USER')", name="ck_provider_interpretation_history_actor_type"),
        UniqueConstraint("interpretation_id", "interpretation_version", name="uq_provider_interpretation_history_version"),
    )

    id = Column(Integer, primary_key=True)
    interpretation_id = Column(Integer, ForeignKey("provider_transaction_interpretations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    interpretation_version = Column(Integer, nullable=False)
    transition_type = Column(String(32), nullable=False)
    actor_type = Column(String(16), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=False)
    reason = Column(String(255), nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ProviderTransactionLifecycle(Base):
    __tablename__ = "provider_transaction_lifecycles"
    __table_args__ = (
        CheckConstraint(
            "current_status IN ('PENDING', 'POSTED', 'REVERSED')",
            name="ck_provider_transaction_lifecycles_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_provider_transaction_lifecycles_version_positive",
        ),
        CheckConstraint(
            "(current_status = 'PENDING' AND posted_candidate_id IS NULL AND reversed_candidate_id IS NULL AND canonical_event_id IS NULL AND reversal_event_id IS NULL) OR "
            "(current_status = 'POSTED' AND posted_candidate_id IS NOT NULL AND reversed_candidate_id IS NULL AND reversal_event_id IS NULL) OR "
            "(current_status = 'REVERSED' AND reversed_candidate_id IS NOT NULL AND ((canonical_event_id IS NULL AND reversal_event_id IS NULL) OR (canonical_event_id IS NOT NULL AND reversal_event_id IS NOT NULL)))",
            name="ck_provider_transaction_lifecycles_state_shape",
        ),
        UniqueConstraint(
            "external_transaction_record_id",
            name="uq_provider_transaction_lifecycles_external_transaction",
        ),
        UniqueConstraint(
            "canonical_event_id",
            name="uq_provider_transaction_lifecycles_canonical_event",
        ),
        UniqueConstraint(
            "reversal_event_id",
            name="uq_provider_transaction_lifecycles_reversal_event",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    external_transaction_record_id = Column(
        Integer,
        ForeignKey("external_transactions.id"),
        nullable=False,
        index=True,
    )
    current_status = Column(String(16), nullable=False)
    current_candidate_id = Column(
        Integer,
        ForeignKey("provider_normalized_candidates.id"),
        nullable=False,
        index=True,
    )
    current_observed_at = Column(DateTime(timezone=True), nullable=False)
    posted_candidate_id = Column(
        Integer,
        ForeignKey("provider_normalized_candidates.id"),
        nullable=True,
        index=True,
    )
    reversed_candidate_id = Column(
        Integer,
        ForeignKey("provider_normalized_candidates.id"),
        nullable=True,
        index=True,
    )
    canonical_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=True,
        index=True,
    )
    reversal_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=True,
        index=True,
    )
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderTransactionLifecycleHistory(Base):
    __tablename__ = "provider_transaction_lifecycle_history"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_version >= 1",
            name="ck_provider_transaction_lifecycle_history_version_positive",
        ),
        CheckConstraint(
            "transition_type IN ('INITIALIZED', 'ADVANCED', 'REFRESHED', 'CANONICAL_LINKED')",
            name="ck_provider_transaction_lifecycle_history_transition_type",
        ),
        UniqueConstraint(
            "lifecycle_id",
            "lifecycle_version",
            name="uq_provider_transaction_lifecycle_history_version",
        ),
    )

    id = Column(Integer, primary_key=True)
    lifecycle_id = Column(
        Integer,
        ForeignKey("provider_transaction_lifecycles.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    lifecycle_version = Column(Integer, nullable=False)
    transition_type = Column(String(32), nullable=False)
    source_candidate_id = Column(
        Integer,
        ForeignKey("provider_normalized_candidates.id"),
        nullable=False,
        index=True,
    )
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=False)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderSyncCheckpoint(Base):
    __tablename__ = "provider_sync_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="ck_provider_sync_checkpoints_version_positive",
        ),
        UniqueConstraint(
            "provider_connection_id",
            name="uq_provider_sync_checkpoints_connection",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_connection_id = Column(
        Integer,
        ForeignKey("provider_connections.id"),
        nullable=False,
        index=True,
    )
    committed_cursor = Column(String(1024), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderSyncPage(Base):
    __tablename__ = "provider_sync_pages"
    __table_args__ = (
        CheckConstraint(
            "length(page_hash) = 64",
            name="ck_provider_sync_pages_hash_length",
        ),
        CheckConstraint(
            "observations_count >= 0 AND evidence_created >= 0 AND evidence_deduplicated >= 0",
            name="ck_provider_sync_pages_counts_nonnegative",
        ),
        CheckConstraint(
            "observations_count = evidence_created + evidence_deduplicated",
            name="ck_provider_sync_pages_counts_balance",
        ),
        CheckConstraint(
            "checkpoint_version_before >= 1 AND checkpoint_version_after = checkpoint_version_before + 1",
            name="ck_provider_sync_pages_checkpoint_version_step",
        ),
        UniqueConstraint(
            "provider_connection_id",
            "page_hash",
            name="uq_provider_sync_pages_connection_hash",
        ),
        UniqueConstraint(
            "provider_connection_id",
            "checkpoint_version_after",
            name="uq_provider_sync_pages_connection_checkpoint_version",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_connection_id = Column(
        Integer,
        ForeignKey("provider_connections.id"),
        nullable=False,
        index=True,
    )
    request_cursor = Column(String(1024), nullable=True)
    request_cursor_key = Column(String(1024), nullable=False)
    next_cursor = Column(String(1024), nullable=True)
    has_more = Column(Boolean, nullable=False)
    page_hash = Column(String(64), nullable=False)
    observations_count = Column(Integer, nullable=False)
    evidence_created = Column(Integer, nullable=False)
    evidence_deduplicated = Column(Integer, nullable=False)
    checkpoint_version_before = Column(Integer, nullable=False)
    checkpoint_version_after = Column(Integer, nullable=False)
    committed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class ProviderSyncPageEvidence(Base):
    __tablename__ = "provider_sync_page_evidence"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0",
            name="ck_provider_sync_page_evidence_ordinal_nonnegative",
        ),
        UniqueConstraint(
            "sync_page_id",
            "ordinal",
            name="uq_provider_sync_page_evidence_page_ordinal",
        ),
    )

    id = Column(Integer, primary_key=True)
    sync_page_id = Column(
        Integer,
        ForeignKey("provider_sync_pages.id"),
        nullable=False,
        index=True,
    )
    external_evidence_id = Column(
        Integer,
        ForeignKey("external_transaction_evidence.id"),
        nullable=False,
        index=True,
    )
    ordinal = Column(Integer, nullable=False)
    created_evidence = Column(Boolean, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (
        CheckConstraint(
            "command_type IN ('CREATE_TRANSACTION', 'UPDATE_TRANSACTION', 'DELETE_TRANSACTION', 'CREATE_TRANSFER', 'CREATE_REFUND', 'CREATE_REVERSAL', 'CREATE_RECONCILIATION', 'RESOLVE_RECONCILIATION', 'CONFIRM_RECONCILIATION_ADJUSTMENT', 'CREATE_PROVIDER_CONNECTION', 'INGEST_EXTERNAL_EVIDENCE', 'NORMALIZE_EXTERNAL_TRANSACTION', 'CLASSIFY_EXTERNAL_TRANSACTION', 'CONFIRM_EXTERNAL_INTERPRETATION')",
            name="ck_command_receipts_command_type",
        ),
        UniqueConstraint(
            "user_id",
            "command_type",
            "idempotency_key",
            name="uq_command_receipts_user_command_key",
        ),
        CheckConstraint(
            "(transaction_id IS NOT NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NOT NULL AND reconciliation_id IS NULL AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NOT NULL AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL AND provider_connection_id IS NOT NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL AND provider_connection_id IS NULL AND external_evidence_id IS NOT NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NOT NULL AND provider_interpretation_id IS NULL) OR "
            "(transaction_id IS NULL AND financial_event_id IS NULL AND reconciliation_id IS NULL AND provider_connection_id IS NULL AND external_evidence_id IS NULL AND normalized_candidate_id IS NULL AND provider_interpretation_id IS NOT NULL)",
            name="ck_command_receipts_exactly_one_target",
        ),
        Index(
            "ix_command_receipts_user_created",
            "user_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    command_type = Column(String, nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=True,
        index=True,
    )
    financial_event_id = Column(
        Integer,
        ForeignKey("financial_events.id"),
        nullable=True,
        index=True,
    )
    reconciliation_id = Column(
        Integer,
        ForeignKey("reconciliation_cases.id"),
        nullable=True,
        index=True,
    )
    provider_connection_id = Column(
        Integer,
        ForeignKey("provider_connections.id"),
        nullable=True,
        index=True,
    )
    external_evidence_id = Column(
        Integer,
        ForeignKey("external_transaction_evidence.id"),
        nullable=True,
        index=True,
    )
    normalized_candidate_id = Column(
        Integer,
        ForeignKey("provider_normalized_candidates.id"),
        nullable=True,
        index=True,
    )
    provider_interpretation_id = Column(
        Integer,
        ForeignKey("provider_transaction_interpretations.id"),
        nullable=True,
        index=True,
    )
    response_status = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )



class FinancialProjectionState(Base):
    __tablename__ = "financial_projection_state"
    __table_args__ = (
        CheckConstraint(
            "generation >= 1",
            name="ck_financial_projection_state_generation_positive",
        ),
        CheckConstraint(
            "length(canonical_fingerprint) = 64",
            name="ck_financial_projection_state_fingerprint_length",
        ),
        CheckConstraint(
            "account_count >= 0",
            name="ck_financial_projection_state_account_count_nonnegative",
        ),
        UniqueConstraint(
            "user_id",
            name="uq_financial_projection_state_user",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    generation = Column(Integer, nullable=False, default=1)
    canonical_fingerprint = Column(String(64), nullable=False)
    total_income = Column(Numeric(18, 2), nullable=False, default=0)
    total_expense = Column(Numeric(18, 2), nullable=False, default=0)
    economic_balance = Column(Numeric(18, 2), nullable=False, default=0)
    net_worth = Column(Numeric(18, 2), nullable=False, default=0)
    account_count = Column(Integer, nullable=False, default=0)
    rebuilt_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


class FinancialAccountBalanceProjection(Base):
    __tablename__ = "financial_account_balance_projections"
    __table_args__ = (
        CheckConstraint(
            "generation >= 1",
            name="ck_financial_account_balance_projections_generation_positive",
        ),
        UniqueConstraint(
            "user_id",
            "account_id",
            name="uq_financial_account_balance_projections_user_account",
        ),
        Index(
            "ix_financial_account_balance_projections_user_generation",
            "user_id",
            "generation",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )
    generation = Column(Integer, nullable=False)
    balance = Column(Numeric(18, 2), nullable=False, default=0)
    rebuilt_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
