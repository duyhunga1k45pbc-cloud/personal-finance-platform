from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from app.recovery import (
    RecoveryError,
    clear_projection_rows,
    create_backup,
    load_manifest,
    restore_backup,
    verify_truth_without_projections,
)


def _run_module(module: str, *, database_url: str, capture: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", module],
        env=env,
        check=True,
        text=True,
        capture_output=capture,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Perform a real backup/restore disaster-recovery drill: same-snapshot backup, "
            "destructive restore into a disposable target, exact state verification, "
            "projection deletion/rebuild, then canonical audit."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/personal-finance-recovery-drills"),
    )
    args = parser.parse_args()

    source_url = os.getenv("DATABASE_URL")
    target_url = os.getenv("RESTORE_DATABASE_URL")
    if not source_url:
        raise SystemExit("DATABASE_URL is required")
    if not target_url:
        raise SystemExit("RESTORE_DATABASE_URL is required")

    started = time.perf_counter()
    try:
        dump_path, manifest_path, manifest = create_backup(
            database_url=source_url,
            output_dir=args.output_dir,
        )
        restore_backup(
            dump_path=dump_path,
            manifest_path=manifest_path,
            target_database_url=target_url,
        )

        # Projections are deliberately disposable. Prove that deleting them after
        # restore cannot alter durable truth and that they rebuild cleanly.
        clear_projection_rows(database_url=target_url)
        verify_truth_without_projections(
            database_url=target_url,
            expected_sha256=manifest["truth_without_projections_sha256"],
        )

        _run_module("scripts.rebuild_financial_projections", database_url=target_url)
        verify_truth_without_projections(
            database_url=target_url,
            expected_sha256=manifest["truth_without_projections_sha256"],
        )

        audit = _run_module(
            "scripts.audit_canonical_parity",
            database_url=target_url,
            capture=True,
        )
        audit_rows = json.loads(audit.stdout)
        if not all(row.get("ok") for row in audit_rows):
            print(audit.stdout)
            raise RecoveryError("Canonical audit failed after restore and projection rebuild")

    except (RecoveryError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"drill_failed: {exc}")
        return 1

    elapsed = time.perf_counter() - started
    print(f"drill_backup={dump_path}")
    print(f"drill_manifest={manifest_path}")
    print(f"drill_database_sha256={manifest['database_sha256']}")
    print(f"drill_truth_sha256={manifest['truth_without_projections_sha256']}")
    print(f"recovery_seconds={elapsed:.3f}")
    print("restore_exact_match=true")
    print("projection_rebuild_verified=true")
    print("canonical_audit_ok=true")
    print(
        "note=restore target is intentionally left in place for inspection; "
        "the next drill safely recreates it"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
