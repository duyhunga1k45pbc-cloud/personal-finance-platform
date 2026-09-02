from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.recovery import RecoveryError, create_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a PostgreSQL custom-format backup plus a same-snapshot integrity manifest."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/personal-finance-backups"),
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    try:
        dump_path, manifest_path, manifest = create_backup(
            database_url=database_url,
            output_dir=args.output_dir,
        )
    except RecoveryError as exc:
        print(f"backup_failed: {exc}")
        return 1

    print(f"backup_dump={dump_path}")
    print(f"backup_manifest={manifest_path}")
    print(f"alembic_revision={manifest['alembic_revision']}")
    print(f"database_sha256={manifest['database_sha256']}")
    print(f"truth_without_projections_sha256={manifest['truth_without_projections_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
