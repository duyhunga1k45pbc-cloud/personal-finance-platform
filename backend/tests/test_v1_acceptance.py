from __future__ import annotations

import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.canonical_audit import audit_user
from app.database import SessionLocal
from app.main import app
from app.models import (
    ExternalTransaction,
    ExternalTransactionEvidence,
    FinancialAccountBalanceProjection,
    FinancialEvent,
    FinancialEventLink,
    FinancialProjectionState,
    ProviderSyncCheckpoint,
    ProviderSyncPage,
)
from app.provider_sync_service import (
    ProviderSyncObservation,
    ProviderSyncPageData,
    commit_sync_page,
    sync_provider_connection,
)


pytestmark = pytest.mark.acceptance
client = TestClient(app)

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 3, 0, 0, tzinfo=UTC)


def create_user(prefix: str = "acceptance"):
    email = f"{prefix}-{uuid4().hex}@example.com"
    password = "testpassword123"
    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200
    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200
    return registered.json()["id"], {
        "Authorization": f"Bearer {logged_in.json()['access_token']}"
    }


def command_headers(headers: dict, *, key: str | None = None, expected_version: int | None = None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
    if expected_version is not None:
        result["X-Expected-Version"] = str(expected_version)
    return result


def create_account(headers: dict, name: str, account_type: str = "BANK"):
    response = client.post(
        "/accounts",
        json={"name": name, "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def default_cash(headers: dict):
    response = client.get("/accounts", headers=headers)
    assert response.status_code == 200
    rows = [row for row in response.json() if row["is_default"]]
    assert len(rows) == 1
    assert rows[0]["account_type"] == "CASH"
    return rows[0]


def create_transaction(
    headers: dict,
    *,
    amount: str,
    transaction_type: str,
    account_id: int,
    description: str,
    key: str | None = None,
):
    response = client.post(
        "/transactions",
        json={
            "amount": amount,
            "description": description,
            "category": "acceptance",
            "type": transaction_type,
            "account_id": account_id,
        },
        headers=command_headers(headers, key=key),
    )
    assert response.status_code == 200
    return response.json()


def create_transfer(headers: dict, *, amount: str, source: int, destination: int, description: str, key: str | None = None):
    response = client.post(
        "/transfers",
        json={
            "amount": amount,
            "from_account_id": source,
            "to_account_id": destination,
            "description": description,
        },
        headers=command_headers(headers, key=key),
    )
    assert response.status_code == 200
    return response.json()


def decimal_field(body: dict, key: str) -> Decimal:
    return Decimal(str(body[key]))


def assert_full_audit_green(user_id: int):
    db = SessionLocal()
    try:
        report = audit_user(db, user_id)
    finally:
        db.close()

    assert report["ok"] is True
    assert report["summary_match"] is True
    divergence_fields = [
        "first_divergence",
        "history_divergence",
        "transfer_divergence",
        "credit_card_divergence",
        "causal_divergence",
        "reconciliation_divergence",
        "provider_evidence_divergence",
        "provider_interpretation_divergence",
        "provider_lifecycle_divergence",
        "provider_sync_divergence",
        "projection_divergence",
    ]
    for field in divergence_fields:
        assert report[field] is None, f"{field}: {report[field]}"
    return report


def create_provider_connection(headers: dict, suffix: str):
    response = client.post(
        "/provider-connections",
        json={
            "provider_name": "MockBank",
            "external_account_id": f"acceptance-{suffix}-{uuid4().hex}",
            "display_name": "V1 acceptance provider",
        },
        headers=command_headers(headers),
    )
    assert response.status_code == 200
    return response.json()


def ingest_provider_evidence(
    headers: dict,
    *,
    connection_id: int,
    external_transaction_id: str,
    observed_at: datetime.datetime,
    status: str,
    amount: str,
):
    response = client.post(
        f"/provider-connections/{connection_id}/external-transactions",
        json={
            "external_transaction_id": external_transaction_id,
            "observed_at": observed_at.isoformat(),
            "raw_payload": {
                "id": external_transaction_id,
                "status": status,
                "amount": amount,
                "currency": "VND",
            },
        },
        headers=command_headers(headers),
    )
    assert response.status_code == 200
    return response.json()


def normalize_provider_evidence(
    headers: dict,
    *,
    connection_id: int,
    transaction_record_id: int,
    evidence_id: int,
    observed_at: datetime.datetime,
    status: str,
    amount: str,
    direction: str,
):
    response = client.post(
        f"/provider-connections/{connection_id}/external-transactions/{transaction_record_id}/normalizations",
        json={
            "evidence_id": evidence_id,
            "normalizer_version": "acceptance-v1",
            "amount": amount,
            "currency": "VND",
            "direction": direction,
            "normalized_status": status,
            "occurred_at": observed_at.isoformat(),
            "description": "acceptance provider transaction",
            "provider_status": status.lower(),
        },
        headers=command_headers(headers),
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("retry_same_command", [True])
def test_v1_household_money_lifecycle_reconciliation_and_projection_rebuild(retry_same_command):
    user_id, headers = create_user("acceptance-household")
    bank = create_account(headers, "Primary Bank", "BANK")
    card = create_account(headers, "Credit Card", "CREDIT_CARD")
    cash = default_cash(headers)

    salary_key = f"salary-{uuid4().hex}"
    salary = create_transaction(
        headers,
        amount="10000000.00",
        transaction_type="income",
        account_id=bank["id"],
        description="monthly salary",
        key=salary_key,
    )
    if retry_same_command:
        replay = create_transaction(
            headers,
            amount="10000000.00",
            transaction_type="income",
            account_id=bank["id"],
            description="monthly salary",
            key=salary_key,
        )
        assert replay == salary

    card_purchase = create_transaction(
        headers,
        amount="1200000.00",
        transaction_type="expense",
        account_id=card["id"],
        description="laptop accessory purchase",
    )
    create_transfer(
        headers,
        amount="1200000.00",
        source=bank["id"],
        destination=card["id"],
        description="credit-card repayment",
    )
    create_transfer(
        headers,
        amount="2000000.00",
        source=bank["id"],
        destination=cash["id"],
        description="ATM withdrawal",
    )
    create_transaction(
        headers,
        amount="300000.00",
        transaction_type="expense",
        account_id=cash["id"],
        description="cash groceries",
    )

    refund = client.post(
        "/refunds",
        json={
            "original_event_id": card_purchase["canonical_event_id"],
            "amount": "200000.00",
            "description": "merchant partial refund",
        },
        headers=command_headers(headers),
    )
    assert refund.status_code == 200
    assert refund.json()["relation_type"] == "REFUND_OF"

    bank_purchase = create_transaction(
        headers,
        amount="100000.00",
        transaction_type="expense",
        account_id=bank["id"],
        description="merchant authorization later voided",
    )
    reversal = client.post(
        "/reversals",
        json={
            "original_event_id": bank_purchase["canonical_event_id"],
            "description": "merchant void",
        },
        headers=command_headers(headers),
    )
    assert reversal.status_code == 200
    assert reversal.json()["relation_type"] == "REVERSAL_OF"

    before_adjustment = client.get("/summary", headers=headers)
    assert before_adjustment.status_code == 200
    before_adjustment = before_adjustment.json()
    assert decimal_field(before_adjustment, "total_income") == Decimal("10000000.00")
    assert decimal_field(before_adjustment, "total_expense") == Decimal("1300000.00")
    assert decimal_field(before_adjustment, "balance") == Decimal("8700000.00")

    reconciliation = client.post(
        "/reconciliations",
        json={
            "account_id": cash["id"],
            "observed_balance": "1500000.00",
            "observed_at": (T0 + datetime.timedelta(hours=8)).isoformat(),
            "note": "physical cash count",
        },
        headers=command_headers(headers),
    )
    assert reconciliation.status_code == 200
    case = reconciliation.json()
    assert case["status"] == "MISMATCH"
    assert Decimal(case["expected_balance"]) == Decimal("1700000.00")
    assert Decimal(case["difference"]) == Decimal("-200000.00")

    adjustment = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json={
            "confirm": True,
            "reason": "unknown historical cash discrepancy confirmed in acceptance scenario",
        },
        headers=command_headers(headers, expected_version=case["version"]),
    )
    assert adjustment.status_code == 200
    assert adjustment.json()["resolution_type"] == "ADJUSTMENT"
    assert Decimal(adjustment.json()["adjustment_amount"]) == Decimal("-200000.00")

    after_adjustment = client.get("/summary", headers=headers).json()
    assert after_adjustment == before_adjustment

    first_projection = client.post("/projections/rebuild", headers=headers)
    assert first_projection.status_code == 200
    first_projection = first_projection.json()
    assert decimal_field(first_projection, "total_income") == Decimal("10000000.00")
    assert decimal_field(first_projection, "total_expense") == Decimal("1300000.00")
    assert decimal_field(first_projection, "balance") == Decimal("8700000.00")
    assert decimal_field(first_projection, "net_worth") == Decimal("8500000.00")

    projected_accounts = client.get("/projections/accounts", headers=headers)
    assert projected_accounts.status_code == 200
    balances = {
        row["account_id"]: Decimal(str(row["balance"]))
        for row in projected_accounts.json()
    }
    assert balances[bank["id"]] == Decimal("6800000.00")
    assert balances[card["id"]] == Decimal("200000.00")
    assert balances[cash["id"]] == Decimal("1500000.00")
    assert sum(balances.values(), Decimal("0")) == Decimal("8500000.00")

    # Destroy every derived projection row. Canonical truth must remain sufficient
    # to reconstruct the same financially meaningful read model.
    db = SessionLocal()
    try:
        db.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).delete(synchronize_session=False)
        db.query(FinancialProjectionState).filter(
            FinancialProjectionState.user_id == user_id
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    assert client.get("/projections/status", headers=headers).json()["status"] == "MISSING"
    rebuilt = client.post("/projections/rebuild", headers=headers)
    assert rebuilt.status_code == 200
    rebuilt = rebuilt.json()
    for field in ("total_income", "total_expense", "balance", "net_worth"):
        assert Decimal(str(rebuilt[field])) == Decimal(str(first_projection[field]))

    report = assert_full_audit_green(user_id)
    assert report["economic_summary"] == {
        "total_income": "10000000.00",
        "total_expense": "1300000.00",
        "balance": "8700000.00",
    }


def test_v1_provider_pending_posted_reversed_preserves_evidence_and_causal_truth():
    user_id, headers = create_user("acceptance-provider")
    bank = create_account(headers, "Provider Bank", "BANK")
    connection = create_provider_connection(headers, "lifecycle")
    external_id = f"provider-{uuid4().hex}"

    pending_time = T0 + datetime.timedelta(minutes=1)
    pending = ingest_provider_evidence(
        headers,
        connection_id=connection["id"],
        external_transaction_id=external_id,
        observed_at=pending_time,
        status="PENDING",
        amount="75000.00",
    )
    pending_norm = normalize_provider_evidence(
        headers,
        connection_id=connection["id"],
        transaction_record_id=pending["external_transaction_record_id"],
        evidence_id=pending["evidence_id"],
        observed_at=pending_time,
        status="PENDING",
        amount="75000.00",
        direction="OUTFLOW",
    )
    pending_interp = pending_norm["interpretation"]
    assert pending_interp["state"] == "UNCLASSIFIED"

    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{pending['external_transaction_record_id']}/confirm",
        json={
            "event_type": "EXPENSE",
            "account_id": bank["id"],
            "reason": "provider purchase confirmed by user",
        },
        headers=command_headers(headers, expected_version=pending_interp["version"]),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "USER_CONFIRMED"
    assert confirmed.json()["canonical_event_id"] is None
    assert confirmed.json()["materialization_blocker"] == "NOT_POSTED"

    posted_time = T0 + datetime.timedelta(minutes=5)
    posted = ingest_provider_evidence(
        headers,
        connection_id=connection["id"],
        external_transaction_id=external_id,
        observed_at=posted_time,
        status="POSTED",
        amount="75000.00",
    )
    posted_norm = normalize_provider_evidence(
        headers,
        connection_id=connection["id"],
        transaction_record_id=posted["external_transaction_record_id"],
        evidence_id=posted["evidence_id"],
        observed_at=posted_time,
        status="POSTED",
        amount="75000.00",
        direction="OUTFLOW",
    )
    assert posted_norm["interpretation_locked"] is False

    interpretation = client.get(
        f"/provider-connections/{connection['id']}/external-transactions/{posted['external_transaction_record_id']}/interpretation",
        headers=headers,
    )
    assert interpretation.status_code == 200
    interpretation = interpretation.json()
    assert interpretation["state"] == "USER_CONFIRMED"
    original_event_id = interpretation["canonical_event_id"]
    assert original_event_id is not None

    summary_after_posted = client.get("/summary", headers=headers).json()
    assert decimal_field(summary_after_posted, "total_expense") == Decimal("75000.00")

    reversed_time = T0 + datetime.timedelta(minutes=9)
    reversed_evidence = ingest_provider_evidence(
        headers,
        connection_id=connection["id"],
        external_transaction_id=external_id,
        observed_at=reversed_time,
        status="REVERSED",
        amount="75000.00",
    )
    reversed_norm = normalize_provider_evidence(
        headers,
        connection_id=connection["id"],
        transaction_record_id=reversed_evidence["external_transaction_record_id"],
        evidence_id=reversed_evidence["evidence_id"],
        observed_at=reversed_time,
        status="REVERSED",
        amount="75000.00",
        direction="OUTFLOW",
    )
    assert reversed_norm["interpretation_locked"] is True

    lifecycle = client.get(
        f"/provider-connections/{connection['id']}/external-transactions/{posted['external_transaction_record_id']}/lifecycle",
        headers=headers,
    )
    assert lifecycle.status_code == 200
    assert lifecycle.json()["current_status"] == "REVERSED"

    db = SessionLocal()
    try:
        original = db.query(FinancialEvent).filter(FinancialEvent.id == original_event_id).one()
        assert original.event_type == "EXPENSE"
        assert original.provenance == "PROVIDER"
        reversals = (
            db.query(FinancialEvent)
            .join(FinancialEventLink, FinancialEventLink.from_event_id == FinancialEvent.id)
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "REVERSAL",
                FinancialEventLink.to_event_id == original_event_id,
                FinancialEventLink.relation_type == "REVERSAL_OF",
            )
            .all()
        )
        assert len(reversals) == 1
        evidence_count = db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.external_transaction_record_id
            == posted["external_transaction_record_id"]
        ).count()
        assert evidence_count == 3
    finally:
        db.close()

    summary_after_reversal = client.get("/summary", headers=headers).json()
    assert decimal_field(summary_after_reversal, "total_expense") == Decimal("0")
    assert decimal_field(summary_after_reversal, "balance") == Decimal("0")
    assert_full_audit_green(user_id)


class ScriptedAdapter:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.calls = []

    def fetch_page(self, cursor):
        self.calls.append(cursor)
        result = self.pages[cursor]
        if isinstance(result, BaseException):
            raise result
        return result


def sync_observation(external_id: str, minute: int):
    return ProviderSyncObservation(
        external_transaction_id=external_id,
        observed_at=T0 + datetime.timedelta(minutes=minute),
        raw_payload={
            "id": external_id,
            "status": "posted",
            "amount": "50000.00",
            "currency": "VND",
        },
    )


def test_v1_provider_sync_crash_restart_and_lost_ack_replay_are_safe():
    user_id, headers = create_user("acceptance-sync")
    connection = create_provider_connection(headers, "sync")

    page1 = ProviderSyncPageData(
        observations=[sync_observation("sync-txn-1", 1)],
        next_cursor="c1",
        has_more=True,
    )
    crash_adapter = ScriptedAdapter(
        {
            None: page1,
            "c1": RuntimeError("simulated provider/worker crash after page 1"),
        }
    )
    with pytest.raises(RuntimeError, match="simulated provider/worker crash"):
        sync_provider_connection(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            adapter=crash_adapter,
        )

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.committed_cursor == "c1"
        assert checkpoint.version == 2
        assert db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).count() == 1
        assert db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.provider_connection_id == connection["id"]
        ).count() == 1
    finally:
        db.close()

    page2 = ProviderSyncPageData(
        observations=[sync_observation("sync-txn-2", 2)],
        next_cursor="c2",
        has_more=False,
    )
    restart_adapter = ScriptedAdapter({"c1": page2})
    resumed = sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=restart_adapter,
    )
    assert restart_adapter.calls == ["c1"]
    assert resumed.start_cursor == "c1"
    assert resumed.end_cursor == "c2"
    assert resumed.pages_committed == 1

    # Simulate a lost acknowledgement for page 1: retrying the exact already-
    # committed page must replay the stored page instead of advancing the cursor.
    replay = commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=page1,
    )
    assert replay.replayed is True

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.committed_cursor == "c2"
        assert checkpoint.version == 3
        assert db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).count() == 2
        assert db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.provider_connection_id == connection["id"]
        ).count() == 2
        assert db.query(ExternalTransaction).filter(
            ExternalTransaction.provider_connection_id == connection["id"]
        ).count() == 2
    finally:
        db.close()

    assert_full_audit_green(user_id)


def test_v1_user_isolation_holds_across_money_provider_and_projection_boundaries():
    user_a, headers_a = create_user("acceptance-owner-a")
    user_b, headers_b = create_user("acceptance-owner-b")
    bank_a = create_account(headers_a, "A bank", "BANK")
    connection_a = create_provider_connection(headers_a, "owner-a")
    create_transaction(
        headers_a,
        amount="123000.00",
        transaction_type="income",
        account_id=bank_a["id"],
        description="owner A income",
    )
    assert client.post("/projections/rebuild", headers=headers_a).status_code == 200

    # Financial account identity is hidden across users.
    assert client.get(f"/accounts/{bank_a['id']}", headers=headers_b).status_code == 404
    # Provider connection identity is hidden across users.
    assert client.get(
        f"/provider-connections/{connection_a['id']}", headers=headers_b
    ).status_code == 404
    # User B's projection remains independent and cannot expose A's financial state.
    assert client.get("/projections/status", headers=headers_b).json()["status"] == "MISSING"
    assert client.get("/summary", headers=headers_b).json() == {
        "total_income": 0,
        "total_expense": 0,
        "balance": 0,
    }

    assert_full_audit_green(user_a)
    assert_full_audit_green(user_b)
