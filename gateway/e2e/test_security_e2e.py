import os
import time

import psycopg2
import requests
import pytest


GATEWAY_URL = os.getenv("E2E_GATEWAY_URL", "http://127.0.0.1:18004")
KEYCLOAK_URL = os.getenv("E2E_KEYCLOAK_URL", "http://127.0.0.1:18081")
DATABASE_URL = os.environ["E2E_DATABASE_URL"]

assert DATABASE_URL.endswith("/finance_e2e_db"), "Refusing non-E2E database"


def get_token():
    url = (
        f"{KEYCLOAK_URL}/realms/pfp-e2e/"
        "protocol/openid-connect/token"
    )

    response = requests.post(
        url,
        data={
            "grant_type": "password",
            "client_id": "moneta",
            "username": "e2e-user",
            "password": "e2e-test-password",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def account_snapshot():
    with psycopg2.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, user_id, name, account_type,
                       currency, is_default, version
                FROM financial_accounts
                ORDER BY id
                """
            )
            return cursor.fetchall()


def test_unauthorized_write_does_not_mutate_database():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    deadline = time.monotonic() + 120

    while True:
        try:
            read = requests.get(
                f"{GATEWAY_URL}/accounts",
                headers=headers,
                timeout=5,
            )
            if read.status_code == 200:
                break
            if read.status_code not in (502, 503, 504):
                pytest.fail(
                    f"Unexpected gateway response: "
                    f"{read.status_code} {read.text}"
                )
        except requests.ConnectionError:
            pass

        if time.monotonic() >= deadline:
            pytest.fail("Gateway did not become ready within 120s")

        time.sleep(2)

    assert read.status_code == 200

    before = account_snapshot()

    denied = requests.post(
        f"{GATEWAY_URL}/accounts",
        headers=headers,
        json={
            "name": "Unauthorized E2E Account",
            "account_type": "BANK",
            "currency": "VND",
        },
        timeout=15,
    )
    assert denied.status_code == 403, denied.text

    after = account_snapshot()

    assert after == before, (
        "Unauthorized request modified financial_accounts"
    )
