import json
import sys

from app.canonical_audit import audit_all_users
from app.database import SessionLocal


def main() -> int:
    db = SessionLocal()
    try:
        results = audit_all_users(db)
    finally:
        db.close()

    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
