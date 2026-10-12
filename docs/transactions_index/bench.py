"""Replay the SQL of GET /transactions (count + ordered page) under EXPLAIN (ANALYZE, BUFFERS).

DISPOSABLE seeded database only (see docs/POSTGRES_INDEX_AUDIT.md on branch perf/index-audit for the seed).
    DATABASE_URL=... python docs/transactions_index/bench.py [--runs 9] [--plans]
The statements come from the application's own query builder via a before_cursor_execute hook.
"""
import argparse, os, re, statistics, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models import Transaction
from app.routers.transactions import _active_transaction_query

ap = argparse.ArgumentParser(); ap.add_argument("--runs", type=int, default=9); ap.add_argument("--plans", action="store_true")
args = ap.parse_args()
engine = create_engine(os.environ["DATABASE_URL"]); Session = sessionmaker(bind=engine)
cap = []
@event.listens_for(engine, "before_cursor_execute")
def _c(conn, cur, st, par, ctx, many):
    if st.lstrip().upper().startswith("SELECT"): cap.append((st, dict(par)))

def case(label, user, skip, limit=10, type_=None):
    cap.clear(); db = Session()
    q = _active_transaction_query(db, user)
    if type_: q = q.filter(Transaction.type == type_)
    q.count(); q.order_by(Transaction.id.asc()).offset(skip).limit(limit).all(); db.close()
    for name, (sql, par) in zip(("count", "page "), cap):
        raw = engine.raw_connection(); cur = raw.cursor(); ts = []
        for _ in range(args.runs):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, par); plan = "\n".join(r[0] for r in cur.fetchall())
            ts.append(float(re.search(r"Execution Time: ([\d.]+)", plan).group(1)))
        top = plan.split("Planning:")[0]
        buf = re.search(r"shared hit=(\d+)(?: read=(\d+))?", top)
        nodes = ",".join(sorted(set(re.findall(r"(Seq Scan|Index Only Scan|Index Scan|Bitmap Heap Scan)", plan))))
        print(f"{label:28s} {name} median={statistics.median(ts):8.2f}ms min={min(ts):.2f} max={max(ts):.2f} "
              f"buffers={int(buf.group(1)) + int(buf.group(2) or 0)} nodes={nodes}")
        if args.plans: print(plan, "\n")
        raw.close()

case("light user(100) p1", 500, 0); case("light user(100) skip 50", 500, 50)
case("heavy user(10k) p1", 5, 0); case("heavy user(10k) skip 5000", 5, 5000)
case("whale(100k) p1", 1, 0); case("whale(100k) skip 50000", 1, 50000); case("whale p1 type=expense", 1, 0, type_="expense")
