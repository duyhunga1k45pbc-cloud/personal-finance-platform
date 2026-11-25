"""Pure contract tests; integration/freshness behavior lives separately."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.analysis_context import build_analysis_context


def projection(**overrides):
    values = {
        "generation": 3,
        "canonical_fingerprint": "a" * 64,
        "total_income": Decimal("100.00"),
        "total_expense": Decimal("40.00"),
        "economic_balance": Decimal("60.00"),
        "net_worth": Decimal("65.00"),
        "account_count": 2,
        "rebuilt_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_context_contains_only_approved_snapshot_bound_facts():
    result = build_analysis_context(
        user_id=17,
        state=projection(),
        checked_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    assert result["schema_version"] == 2
    assert result["snapshot_id"].startswith("snap_v2_")
    assert result["reporting_period"] == {
        "kind": "ALL_TIME",
        "start": None,
        "end": None,
        "timezone": "UTC",
    }
    assert result["metrics"] == {
        "total_income": "100.00",
        "total_expense": "40.00",
        "balance": "60.00",
        "net_worth": "65.00",
        "account_count": 2,
        "expense_to_income_percent": "40.00",
    }
    assert len(result["facts"]) == len(result["evidence"]) == 6
    for fact, evidence in zip(result["facts"], result["evidence"], strict=True):
        assert evidence["snapshot_id"] == result["snapshot_id"]
        assert evidence["fact_id"] == fact["id"]
        assert evidence["source"] == "financial_projection_state"
    assert "user_id" not in result
    assert "transactions" not in result
    assert "description" not in result


def test_snapshot_fact_and_evidence_ids_are_deterministic_and_owner_scoped():
    state = projection()
    one = build_analysis_context(user_id=17, state=state)
    repeated = build_analysis_context(user_id=17, state=state)
    other_owner = build_analysis_context(user_id=18, state=state)
    changed_generation = build_analysis_context(
        user_id=17, state=projection(generation=4)
    )

    assert one["snapshot_id"] == repeated["snapshot_id"]
    assert [fact["id"] for fact in one["facts"]] == [
        fact["id"] for fact in repeated["facts"]
    ]
    assert [item["id"] for item in one["evidence"]] == [
        item["id"] for item in repeated["evidence"]
    ]
    assert one["snapshot_id"] != other_owner["snapshot_id"]
    assert one["snapshot_id"] != changed_generation["snapshot_id"]
    assert one["evidence"][0]["id"] != other_owner["evidence"][0]["id"]


def test_empty_projection_has_zero_totals_and_undefined_ratio():
    result = build_analysis_context(
        user_id=23,
        state=projection(
            total_income=Decimal("0.00"),
            total_expense=Decimal("0.00"),
            economic_balance=Decimal("0.00"),
            net_worth=Decimal("0.00"),
            account_count=0,
        ),
    )

    assert result["metrics"] == {
        "total_income": "0.00",
        "total_expense": "0.00",
        "balance": "0.00",
        "net_worth": "0.00",
        "account_count": 0,
        "expense_to_income_percent": None,
    }
    ratio_fact = next(f for f in result["facts"] if f["metric"] == "expense_to_income_percent")
    assert ratio_fact["value"] is None
    assert ratio_fact["availability"] == "UNDEFINED_ZERO_DENOMINATOR"


def test_zero_income_with_expenses_does_not_invent_a_percentage():
    result = build_analysis_context(
        user_id=24,
        state=projection(
            total_income=Decimal("0.00"),
            total_expense=Decimal("25.00"),
            economic_balance=Decimal("-25.00"),
        ),
    )

    assert result["metrics"]["expense_to_income_percent"] is None
    ratio_fact = next(f for f in result["facts"] if f["metric"] == "expense_to_income_percent")
    assert ratio_fact["value"] is None


def test_expense_to_income_percentage_rounds_half_up_to_two_decimals():
    result = build_analysis_context(
        user_id=25,
        state=projection(
            total_income=Decimal("32.00"),
            total_expense=Decimal("1.00"),
            economic_balance=Decimal("31.00"),
        ),
    )

    assert result["metrics"]["expense_to_income_percent"] == "3.13"
