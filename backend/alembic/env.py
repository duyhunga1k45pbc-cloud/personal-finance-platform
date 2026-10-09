from logging.config import fileConfig

import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy.engine import make_url

from alembic import context

config = context.config

database_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not database_url:
    raise RuntimeError("DATABASE_URL must be explicitly configured for migrations")

database_name = make_url(database_url).database
if database_name is None:
    raise RuntimeError("Refusing to run migrations without a database name.")
if (
    database_name.lower() == "finance_db"
    and context.get_x_argument(as_dictionary=True).get("allow_production_migrations")
    != "finance_db"
):
    raise RuntimeError(
        "Refusing to migrate finance_db without explicit per-invocation "
        "authorization: -x allow_production_migrations=finance_db."
    )

os.environ["DATABASE_URL"] = database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _target_metadata():
    from app.database import Base
    from app import models

    return Base.metadata


target_metadata = _target_metadata()

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            connected_database = connection.exec_driver_sql(
                "SELECT current_database()"
            ).scalar_one()
            if connected_database != database_name:
                raise RuntimeError(
                    "Refusing to run migrations: connected database identity does "
                    "not match the configured database."
                )
            connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
