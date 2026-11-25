"""Versioned, owner-scoped financial facts for downstream analysis."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ALL_TIME = "ALL_TIME"
MONEY_QUANTUM = Decimal("0.01")

_METRIC_DEFINITIONS = (
    (
        "total_income",
        "MONEY",
        "VND",
        "All-time sum of active INCOME entries plus linked REVERSAL entries for INCOME originals, without an event-date filter; excludes TRANSFER, EXPENSE, REFUND, and ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
    ),
    (
        "total_expense",
        "MONEY",
        "VND",
        "All-time negated sum of active EXPENSE entries plus linked REFUND and REVERSAL entries for EXPENSE originals, without an event-date filter; excludes TRANSFER, INCOME, and ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
    ),
    (
        "balance",
        "MONEY",
        "VND",
        "Reported economic balance equal to total_income minus total_expense for the all-time period; it does not include ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
    ),
    (
        "net_worth",
        "MONEY",
        "VND",
        "Sum of all active canonical signed account movements, including adjustments and transfers, across all owner accounts; VND is stored at two decimal places and emitted at that precision without further rounding; not a market valuation.",
    ),
    (
        "account_count",
        "COUNT",
        "accounts",
        "Exact integer count of the owner's financial accounts, including accounts with zero balance; no rounding.",
    ),
    (
        "expense_to_income_percent",
        "PERCENTAGE",
        "percent",
        "total_expense divided by total_income multiplied by 100, rounded half-up to two decimal percentage points; null when total_income is zero.",
    ),
)


def _digest(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _money(value: Any) -> str:
    amount = Decimal(value or 0).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return format(amount, ".2f")


def _snapshot_id(user_id: int, generation: int, fingerprint: str) -> str:
    return "snap_v2_" + _digest(
        "pfp-analysis-snapshot-v2", str(user_id), str(generation), fingerprint
    )


def _fact_value(metric: str, metrics: dict[str, Any]) -> str | None:
    value = metrics[metric]
    if value is None:
        return None
    if metric in {"total_income", "total_expense", "balance", "net_worth"}:
        return _money(value)
    if metric == "account_count":
        return str(int(value))
    return format(Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), ".2f")


def build_analysis_context(
    *, user_id: int, state: Any, checked_at: datetime | None = None
) -> dict[str, Any]:
    """Build the public v2 contract from a projection already checked as fresh."""
    income = Decimal(state.total_income or 0)
    expense = Decimal(state.total_expense or 0)
    expense_to_income = (
        None
        if income == 0
        else (expense / income * Decimal("100")).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )
    )
    metrics: dict[str, Any] = {
        "total_income": _money(income),
        "total_expense": _money(expense),
        "balance": _money(state.economic_balance),
        "net_worth": _money(state.net_worth),
        "account_count": int(state.account_count),
        "expense_to_income_percent": (
            None if expense_to_income is None else format(expense_to_income, ".2f")
        ),
    }

    generation = int(state.generation)
    fingerprint = str(state.canonical_fingerprint)
    snapshot_id = _snapshot_id(user_id, generation, fingerprint)
    facts: list[dict[str, Any]] = []
    evidence: list[dict[str, str]] = []
    for metric, value_type, unit, definition in _METRIC_DEFINITIONS:
        value = _fact_value(metric, metrics)
        availability = (
            "UNDEFINED_ZERO_DENOMINATOR"
            if metric == "expense_to_income_percent" and value is None
            else "AVAILABLE"
        )
        value_token = "<null>" if value is None else value
        fact_id = "fact_v2_" + _digest(
            "pfp-financial-fact-v2",
            snapshot_id,
            metric,
            value_type,
            value_token,
            unit,
            ALL_TIME,
            availability,
        )
        facts.append(
            {
                "id": fact_id,
                "metric": metric,
                "value_type": value_type,
                "value": value,
                "unit": unit,
                "period": ALL_TIME,
                "availability": availability,
                "definition": definition,
            }
        )
        source = "financial_projection_state"
        evidence.append(
            {
                "id": "evidence_v2_" + _digest(
                    "pfp-financial-evidence-v2", snapshot_id, fact_id, source
                ),
                "snapshot_id": snapshot_id,
                "fact_id": fact_id,
                "source": source,
            }
        )

    now = checked_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc).isoformat()
    projected_at = state.rebuilt_at
    if projected_at.tzinfo is None:
        projected_at = projected_at.replace(tzinfo=timezone.utc)

    return {
        "schema_version": 2,
        "analysis_type": "all_time_financial_summary",
        "currency": "VND",
        "period": "all_time",
        "data_status": "FRESH",
        "generation": generation,
        "metrics": metrics,
        "limitations": [
            "All-time totals only; no monthly or category attribution is provided.",
            "Account net worth is the sum of recorded signed movements, not a market valuation.",
            "Expense-to-income percentage is undefined when total income is zero.",
            "These facts do not establish causes or predict future financial outcomes.",
        ],
        "generated_at": now,
        "snapshot_id": snapshot_id,
        "reporting_period": {
            "kind": ALL_TIME,
            "start": None,
            "end": None,
            "timezone": "UTC",
        },
        "facts": facts,
        "evidence": evidence,
        "freshness": {
            "status": "FRESH",
            "generation": generation,
            "canonical_fingerprint": fingerprint,
            "projection_rebuilt_at": projected_at.isoformat(),
            "checked_at": now,
        },
    }
