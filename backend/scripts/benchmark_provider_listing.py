#!/usr/bin/env python3
"""Run a read-only, paired provider-listing benchmark on the approved PFP DB."""

from __future__ import annotations

import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

EXPECTED_DATABASE = "pfp_perf_benchmark"
EXPECTED_ROLE = "pfp_bench_app"
EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 55432
EXPECTED_RECORDS = 1000
SAMPLES = 25
SEED = 20261012


def fail(message: str) -> None:
    raise SystemExit(f"Benchmark safety check failed: {message}")


def safe_database_url():
    value = os.environ.get("DATABASE_URL", "")
    if not value:
        fail("Set DATABASE_URL to the isolated benchmark database.")
    url = make_url(value)
    if (
        url.drivername != "postgresql+psycopg2"
        or url.host != EXPECTED_HOST
        or url.port != EXPECTED_PORT
        or url.database != EXPECTED_DATABASE
        or url.username != EXPECTED_ROLE
    ):
        fail("DATABASE_URL does not identify the approved benchmark target.")
    query = dict(url.query)
    query["options"] = (
        "-c default_transaction_read_only=on "
        "-c statement_timeout=30000 -c lock_timeout=5000"
    )
    return url.set(query=query)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (
        position - lower
    )


def summarize(samples: list[dict]) -> dict:
    result = {"samples": len(samples)}
    for output_name, key in (
        ("request_latency_ms", "latency_ms"),
        ("db_execute_ms", "db_execute_ms"),
        ("sql_statements_per_request", "statements"),
    ):
        values = [float(sample[key]) for sample in samples]
        result[output_name] = {
            "p50": round(percentile(values, 0.50), 3),
            "p95": round(percentile(values, 0.95), 3),
            "mean": round(statistics.fmean(values), 3),
        }
    return result


def main() -> None:
    database_url = safe_database_url()
    os.environ["DATABASE_URL"] = database_url.render_as_string(
        hide_password=False
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    identity_engine = create_engine(
        database_url,
        poolclass=NullPool,
    )
    with identity_engine.connect() as connection:
        identity = connection.exec_driver_sql(
            """
            SELECT current_database(), current_user,
                   current_setting('server_version'),
                   inet_server_port(),
                   current_setting('transaction_read_only')
            """
        ).one()
        if (
            identity[0] != EXPECTED_DATABASE
            or identity[1] != EXPECTED_ROLE
            or not identity[2].startswith("16.")
            or identity[3] != 5432
            or identity[4] != "on"
        ):
            fail(f"Connected database identity mismatch: {identity!r}")
        records = connection.exec_driver_sql(
            "SELECT count(*) FROM external_transactions"
        ).scalar_one()
        evidence = connection.exec_driver_sql(
            "SELECT count(*) FROM external_transaction_evidence"
        ).scalar_one()
        if records != EXPECTED_RECORDS or evidence != EXPECTED_RECORDS:
            fail(
                "Expected 1,000 external transactions and 1,000 evidence rows; "
                f"found {records} and {evidence}."
            )
    identity_engine.dispose()

    from app.auth import create_access_token
    from app.database import SessionLocal, engine
    from app.models import (
        ExternalTransaction,
        ExternalTransactionEvidence,
        ProviderConnection,
        User,
    )
    from app.provider_service import get_owned_provider_connection
    from app.routers.auth import get_current_user
    from app.routers.providers import (
        get_db,
        list_external_transactions,
    )

    with SessionLocal() as db:
        connections = (
            db.query(ProviderConnection)
            .order_by(ProviderConnection.id.asc())
            .limit(2)
            .all()
        )
        if len(connections) != 1:
            fail(f"Expected one provider connection; found {len(connections)}.")
        connection_id = connections[0].id
        user_id = connections[0].user_id

    def legacy_listing(
        connection_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        provider_connection = get_owned_provider_connection(
            db,
            user_id=current_user.id,
            connection_id=connection_id,
        )
        if provider_connection is None:
            raise HTTPException(
                status_code=404,
                detail="Provider connection not found",
            )
        rows = (
            db.query(ExternalTransaction)
            .filter(
                ExternalTransaction.provider_connection_id
                == provider_connection.id
            )
            .order_by(ExternalTransaction.id.asc())
            .all()
        )
        return [
            {
                "id": row.id,
                "external_transaction_id": row.external_transaction_id,
                "first_observed_at": row.first_observed_at,
                "evidence_count": (
                    db.query(ExternalTransactionEvidence)
                    .filter(
                        ExternalTransactionEvidence.external_transaction_record_id
                        == row.id
                    )
                    .count()
                ),
            }
            for row in rows
        ]

    applications = {}
    for name, endpoint in (
        ("legacy", legacy_listing),
        ("set_based", list_external_transactions),
    ):
        application = FastAPI()
        application.add_api_route(
            "/provider-connections/{connection_id}/external-transactions",
            endpoint,
            methods=["GET"],
        )
        applications[name] = application

    metrics = {"active": None, "total_statements": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        sample = metrics["active"]
        if sample is None:
            return
        sample["statements"] += 1
        metrics["total_statements"] += 1
        if sample["statements"] > 1100 or metrics["total_statements"] > 50000:
            fail("Statement safety limit exceeded.")
        context._benchmark_started = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        started = getattr(context, "_benchmark_started", None)
        if started is not None and metrics["active"] is not None:
            metrics["active"]["db_execute_ms"] += (
                time.perf_counter() - started
            ) * 1000

    token = create_access_token({"sub": str(user_id)})
    path = f"/provider-connections/{connection_id}/external-transactions"
    clients = {
        name: TestClient(application)
        for name, application in applications.items()
    }
    observations = {"legacy": [], "set_based": []}

    def request(client: TestClient) -> tuple[list, dict]:
        sample = {"statements": 0, "db_execute_ms": 0.0}
        metrics["active"] = sample
        started = time.perf_counter()
        try:
            response = client.get(
                path,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            sample["latency_ms"] = (time.perf_counter() - started) * 1000
            metrics["active"] = None
        if response.status_code != 200:
            fail(f"Listing returned HTTP {response.status_code}.")
        body = response.json()
        if len(body) != EXPECTED_RECORDS:
            fail(f"Listing returned {len(body)} rows.")
        return body, sample

    try:
        warmup_bodies = {}
        for name in ("legacy", "set_based"):
            warmup_bodies[name], _ = request(clients[name])
        if warmup_bodies["legacy"] != warmup_bodies["set_based"]:
            fail("Warm-up response bodies differ.")

        order = list(range(SAMPLES))
        random.Random(SEED).shuffle(order)
        for pair in order:
            names = (
                ("legacy", "set_based")
                if pair % 2 == 0
                else ("set_based", "legacy")
            )
            pair_bodies = {}
            for name in names:
                pair_bodies[name], sample = request(clients[name])
                observations[name].append(sample)
            if pair_bodies["legacy"] != pair_bodies["set_based"]:
                fail(f"Response bodies differ in paired sample {pair}.")
    finally:
        for client in clients.values():
            client.close()
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)
        engine.dispose()

    expected_statements = {"legacy": 1003, "set_based": 3}
    for name, samples in observations.items():
        if any(
            sample["statements"] != expected_statements[name]
            for sample in samples
        ):
            fail(f"Unexpected SQL statement count for {name}.")

    print(
        json.dumps(
            {
                "format": "pfp-provider-listing-paired-benchmark-v1",
                "target": {
                    "database": identity[0],
                    "role": identity[1],
                    "postgresql": identity[2],
                    "read_only": identity[4],
                },
                "dataset": {
                    "external_transactions": records,
                    "external_transaction_evidence": evidence,
                },
                "method": {
                    "samples_per_implementation": SAMPLES,
                    "warmups_per_implementation": 1,
                    "order": "randomized paired, alternating",
                    "response_equivalence": True,
                    "p99": "not reported for 25 samples",
                },
                "results": {
                    name: summarize(samples)
                    for name, samples in observations.items()
                },
                "total_statements": metrics["total_statements"],
                "transaction_duration": "not measured",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
