import os


def pytest_sessionstart(session):
    database_url = os.getenv("DATABASE_URL", "")
    if "test" not in database_url.lower():
        raise RuntimeError(
            "Refusing to run tests against a non-test database. "
            "Set DATABASE_URL to finance_test_db (or another database containing 'test')."
        )
