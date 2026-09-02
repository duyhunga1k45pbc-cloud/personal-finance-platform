from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.recovery import RecoveryError, restore_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Restore a backup into an explicitly disposable DR/test database and verify "
            "its exact database fingerprint."
        )
    )
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--confirm-destroy-target",
        action="store_true",
        help="Required because the restore target database is dropped and recreated.",
    )
    args = parser.parse_args()

    if not args.confirm_destroy_target:
        raise SystemExit("Refusing restore without --confirm-destroy-target")

    target_url = os.getenv("RESTORE_DATABASE_URL")
    if not target_url:
        raise SystemExit("RESTORE_DATABASE_URL is required")

    try:
        restored = restore_backup(
            dump_path=args.dump,
            manifest_path=args.manifest,
            target_database_url=target_url,
        )
    except RecoveryError as exc:
        print(f"restore_failed: {exc}")
        return 1

    print(f"restore_verified_database_sha256={restored['database_sha256']}")
    print(f"restore_verified_schema_objects_sha256={restored['schema_objects_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
