from __future__ import annotations

import argparse
import json

from app.deployment import deployment_preflight


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed V1 deployment preflight. Does not run migrations."
    )
    parser.add_argument(
        "--skip-canonical-audit",
        action="store_true",
        help="Check connectivity/schema only. Prefer the full audit for releases.",
    )
    args = parser.parse_args()

    result = deployment_preflight(
        run_canonical_audit=not args.skip_canonical_audit,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
