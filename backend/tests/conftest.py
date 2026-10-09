import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def pytest_sessionstart(session):
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError(
            "Refusing to run tests without an explicit test DATABASE_URL."
        )
    url = make_url(database_url)
    database_name = url.database or ""
    if "test" not in database_name.lower() or database_name.lower() == "finance_db":
        raise RuntimeError(
            "Refusing to run tests against a database not explicitly named for testing."
        )

    if url.drivername.startswith("postgresql"):
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                identity = connection.execute(
                    text("SELECT current_database(), current_setting('transaction_read_only')")
                ).one()
                if identity[0] != database_name or identity[1] != "on":
                    raise RuntimeError(
                        "Refusing to run tests: connected PostgreSQL database identity "
                        "does not match the configured test database."
                    )
        finally:
            engine.dispose()
