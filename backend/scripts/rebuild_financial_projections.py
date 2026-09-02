from __future__ import annotations

import argparse

from app.database import SessionLocal
from app.models import User
from app.projection_service import rebuild_user_projection


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild disposable financial read-model projections from canonical state."
    )
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()

    discovery = SessionLocal()
    try:
        if args.user_id is None:
            user_ids = [row[0] for row in discovery.query(User.id).order_by(User.id.asc()).all()]
        else:
            exists = discovery.query(User.id).filter(User.id == args.user_id).scalar()
            if exists is None:
                raise SystemExit(f"User {args.user_id} not found")
            user_ids = [args.user_id]
    finally:
        discovery.close()

    for user_id in user_ids:
        db = SessionLocal()
        try:
            state = rebuild_user_projection(db, user_id)
            print(
                f"user_id={user_id} generation={state.generation} "
                f"accounts={state.account_count} fingerprint={state.canonical_fingerprint}"
            )
        finally:
            db.close()

    print(f"rebuilt={len(user_ids)}")


if __name__ == "__main__":
    main()
