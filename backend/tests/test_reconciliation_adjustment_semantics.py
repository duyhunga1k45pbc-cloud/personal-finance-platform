from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.canonical_audit import audit_user, find_first_reconciliation_divergence
from app.credit_card_service import canonical_account_position
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialEvent,
    FinancialEventEntry,
    ReconciliationCase,
    ReconciliationHistory,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user(prefix="recon"):
    email = f"{prefix}_{uuid4().hex}@example.com"
    password = "testpassword123"
    register = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert register.status_code == 200
    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    return register.json()["id"], {
        "Authorization": f"Bearer {login.json()['access_token']}"
    }


def command_headers(headers, *, key=None, expected_version=None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
    if expected_version is not None:
        result["X-Expected-Version"] = str(expected_version)
    return result


def default_cash(headers):
    response = client.get("/accounts", headers=headers)
    assert response.status_code == 200
    return next(row for row in response.json() if row["is_default"])


def create_account(headers, *, name="Bank", account_type="BANK"):
    response = client.post(
        "/accounts",
        json={"name": name, "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def create_transaction(headers, account_id, *, amount, transaction_type, description):
    response = client.post(
        "/transactions",
        json={
            "amount": amount,
            "description": description,
            "category": "reconciliation-test",
            "type": transaction_type,
            "account_id": account_id,
        },
        headers=command_headers(headers),
    )
    assert response.status_code == 200
    return response.json()


def detect(headers, account_id, observed_balance, *, key=None):
    return client.post(
        "/reconciliations",
        json={
            "account_id": account_id,
            "observed_balance": observed_balance,
            "note": "manual cash count",
        },
        headers=command_headers(headers, key=key),
    )


def test_exact_cash_observation_is_reconciled_and_does_not_create_adjustment():
    user_id, headers = create_user()
    cash = default_cash(headers)

    response = detect(headers, cash["id"], "0.00")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RECONCILED"
    assert Decimal(body["expected_balance"]) == 0
    assert Decimal(body["observed_balance"]) == 0
    assert Decimal(body["difference"]) == 0
    assert body["resolution_type"] is None
    assert body["version"] == 1

    db = SessionLocal()
    try:
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 0
        history = db.query(ReconciliationHistory).filter(
            ReconciliationHistory.reconciliation_id == body["id"]
        ).one()
        assert history.transition_type == "DETECTED"
        assert history.reconciliation_version == 1
        assert audit_user(db, user_id)["reconciliation_divergence"] is None
    finally:
        db.close()


def test_mismatch_detection_is_observation_only_and_never_silently_mutates_cash():
    user_id, headers = create_user()
    cash = default_cash(headers)

    response = detect(headers, cash["id"], "500000.00")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "MISMATCH"
    assert Decimal(body["expected_balance"]) == 0
    assert Decimal(body["observed_balance"]) == Decimal("500000.00")
    assert Decimal(body["difference"]) == Decimal("500000.00")

    db = SessionLocal()
    try:
        assert canonical_account_position(
            db, user_id=user_id, account_id=cash["id"]
        ) == 0
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 0
    finally:
        db.close()


def test_known_missing_expense_resolves_mismatch_as_real_event_not_adjustment():
    user_id, headers = create_user()
    cash = default_cash(headers)
    create_transaction(
        headers,
        cash["id"],
        amount="1000000.00",
        transaction_type="income",
        description="cash funding",
    )

    reconciliation = detect(headers, cash["id"], "700000.00")
    assert reconciliation.status_code == 200
    case = reconciliation.json()
    assert case["status"] == "MISMATCH"
    assert Decimal(case["difference"]) == Decimal("-300000.00")

    create_transaction(
        headers,
        cash["id"],
        amount="300000.00",
        transaction_type="expense",
        description="missing cash expense reconstructed",
    )
    resolved = client.post(
        f"/reconciliations/{case['id']}/resolve",
        json={"reason": "missing expense reconstructed"},
        headers=command_headers(headers, expected_version=case["version"]),
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution_type"] == "REAL_EVENT"
    assert body["adjustment_event_id"] is None
    assert Decimal(body["resolved_balance"]) == Decimal("700000.00")
    assert body["version"] == 2

    db = SessionLocal()
    try:
        assert canonical_account_position(
            db, user_id=user_id, account_id=cash["id"]
        ) == Decimal("700000.00")
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 0
        history = db.query(ReconciliationHistory).filter(
            ReconciliationHistory.reconciliation_id == case["id"]
        ).order_by(ReconciliationHistory.reconciliation_version).all()
        assert [row.transition_type for row in history] == [
            "DETECTED",
            "RESOLVED_REAL_EVENT",
        ]
        assert audit_user(db, user_id)["reconciliation_divergence"] is None
    finally:
        db.close()


def test_explicit_adjustment_closes_unknown_gap_without_disguising_it_as_expense():
    user_id, headers = create_user()
    cash = default_cash(headers)
    create_transaction(
        headers,
        cash["id"],
        amount="1000000.00",
        transaction_type="income",
        description="cash funding",
    )
    reconciliation = detect(headers, cash["id"], "700000.00").json()

    before_summary = client.get("/summary", headers=headers).json()
    adjustment = client.post(
        f"/reconciliations/{reconciliation['id']}/adjustments",
        json={
            "confirm": True,
            "reason": "unknown historical cash discrepancy confirmed by user",
        },
        headers=command_headers(
            headers,
            expected_version=reconciliation["version"],
        ),
    )
    assert adjustment.status_code == 200
    body = adjustment.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution_type"] == "ADJUSTMENT"
    assert body["adjustment_event_type"] == "ADJUSTMENT"
    assert Decimal(body["adjustment_amount"]) == Decimal("-300000.00")
    assert Decimal(body["resolved_balance"]) == Decimal("700000.00")
    assert body["version"] == 2

    after_summary = client.get("/summary", headers=headers).json()
    assert after_summary == before_summary
    assert Decimal(str(after_summary["total_expense"])) == 0
    assert Decimal(str(after_summary["total_income"])) == Decimal("1000000.00")

    db = SessionLocal()
    try:
        event = db.query(FinancialEvent).filter(
            FinancialEvent.id == body["adjustment_event_id"]
        ).one()
        assert event.event_type == "ADJUSTMENT"
        assert event.interpretation_state == "USER_CONFIRMED"
        assert event.confidence == "USER_CONFIRMED"
        assert event.provenance == "USER_MANUAL"
        entry = db.query(FinancialEventEntry).filter(
            FinancialEventEntry.financial_event_id == event.id
        ).one()
        assert entry.account_id == cash["id"]
        assert Decimal(entry.amount) == Decimal("-300000.00")
        assert canonical_account_position(
            db, user_id=user_id, account_id=cash["id"]
        ) == Decimal("700000.00")
        assert audit_user(db, user_id)["reconciliation_divergence"] is None
    finally:
        db.close()


def test_adjustment_requires_explicit_true_confirmation_and_reason():
    user_id, headers = create_user()
    cash = default_cash(headers)
    case = detect(headers, cash["id"], "100000.00").json()

    false_confirmation = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json={"confirm": False, "reason": "unknown gap"},
        headers=command_headers(headers, expected_version=1),
    )
    missing_reason = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json={"confirm": True, "reason": ""},
        headers=command_headers(headers, expected_version=1),
    )
    assert false_confirmation.status_code == 422
    assert missing_reason.status_code == 422

    db = SessionLocal()
    try:
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 0
    finally:
        db.close()


def test_stale_reconciliation_cannot_adjust_after_canonical_state_changes():
    user_id, headers = create_user()
    cash = default_cash(headers)
    create_transaction(
        headers,
        cash["id"],
        amount="1000000.00",
        transaction_type="income",
        description="cash funding",
    )
    case = detect(headers, cash["id"], "700000.00").json()

    create_transaction(
        headers,
        cash["id"],
        amount="100000.00",
        transaction_type="expense",
        description="partial reconstructed expense",
    )
    response = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json={"confirm": True, "reason": "should not use stale observation"},
        headers=command_headers(headers, expected_version=case["version"]),
    )
    assert response.status_code == 409

    db = SessionLocal()
    try:
        stored_case = db.query(ReconciliationCase).filter(
            ReconciliationCase.id == case["id"]
        ).one()
        assert stored_case.status == "MISMATCH"
        assert stored_case.version == 1
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 0
    finally:
        db.close()


def test_real_event_resolution_rejects_while_mismatch_still_exists():
    _, headers = create_user()
    cash = default_cash(headers)
    case = detect(headers, cash["id"], "100000.00").json()

    response = client.post(
        f"/reconciliations/{case['id']}/resolve",
        json={},
        headers=command_headers(headers, expected_version=case["version"]),
    )
    assert response.status_code == 409


def test_task8_reconciliation_is_cash_only_and_cross_user_account_is_hidden():
    _, owner_headers = create_user("owner")
    _, other_headers = create_user("other")
    bank = create_account(owner_headers, name="Bank", account_type="BANK")
    owner_cash = default_cash(owner_headers)

    non_cash = detect(owner_headers, bank["id"], "0.00")
    foreign = detect(other_headers, owner_cash["id"], "0.00")
    assert non_cash.status_code == 422
    assert foreign.status_code == 404


def test_reconciliation_creation_is_idempotent_and_key_cannot_change_observation():
    user_id, headers = create_user()
    cash = default_cash(headers)
    key = uuid4().hex

    first = detect(headers, cash["id"], "100000.00", key=key)
    second = detect(headers, cash["id"], "100000.00", key=key)
    conflict = detect(headers, cash["id"], "200000.00", key=key)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert conflict.status_code == 409

    db = SessionLocal()
    try:
        assert db.query(ReconciliationCase).filter(
            ReconciliationCase.user_id == user_id
        ).count() == 1
        receipt = db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "CREATE_RECONCILIATION",
            CommandReceipt.idempotency_key == key,
        ).one()
        assert receipt.reconciliation_id == first.json()["id"]
        assert receipt.transaction_id is None
        assert receipt.financial_event_id is None
    finally:
        db.close()


def test_adjustment_command_is_idempotent_and_stale_second_writer_is_rejected():
    user_id, headers = create_user()
    cash = default_cash(headers)
    case = detect(headers, cash["id"], "100000.00").json()
    key = uuid4().hex
    payload = {"confirm": True, "reason": "explicit unknown cash gap"}
    request_headers = command_headers(headers, key=key, expected_version=1)

    first = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json=payload,
        headers=request_headers,
    )
    second = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json=payload,
        headers=request_headers,
    )
    stale = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json=payload,
        headers=command_headers(headers, expected_version=1),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert stale.status_code == 409

    db = SessionLocal()
    try:
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        ).count() == 1
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "CONFIRM_RECONCILIATION_ADJUSTMENT",
        ).count() == 1
    finally:
        db.close()


def test_reconciliation_is_user_scoped():
    _, owner_headers = create_user("owner-case")
    _, other_headers = create_user("other-case")
    cash = default_cash(owner_headers)
    case = detect(owner_headers, cash["id"], "100.00").json()

    hidden = client.get(
        f"/reconciliations/{case['id']}",
        headers=other_headers,
    )
    assert hidden.status_code == 404


def test_audit_detects_adjustment_amount_tampering_without_polluting_database():
    user_id, headers = create_user()
    cash = default_cash(headers)
    case = detect(headers, cash["id"], "100000.00").json()
    adjustment = client.post(
        f"/reconciliations/{case['id']}/adjustments",
        json={"confirm": True, "reason": "explicit adjustment"},
        headers=command_headers(headers, expected_version=1),
    )
    assert adjustment.status_code == 200

    db = SessionLocal()
    try:
        entry = db.query(FinancialEventEntry).filter(
            FinancialEventEntry.financial_event_id
            == adjustment.json()["adjustment_event_id"]
        ).one()
        entry.amount = Decimal("1.00")
        db.flush()
        divergence = find_first_reconciliation_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "adjustment_amount_does_not_close_original_mismatch"
        db.rollback()
        assert audit_user(db, user_id)["reconciliation_divergence"] is None
    finally:
        db.rollback()
        db.close()


def test_reconciliation_history_is_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level reconciliation_history trigger is PostgreSQL-specific")

    _, headers = create_user()
    cash = default_cash(headers)
    case = detect(headers, cash["id"], "100.00").json()

    db = SessionLocal()
    try:
        history = db.query(ReconciliationHistory).filter(
            ReconciliationHistory.reconciliation_id == case["id"]
        ).one()
        history.reason = "tampered"
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()
