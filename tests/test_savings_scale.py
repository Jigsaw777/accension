"""Synthetic historical ledger: two million usage rows, indexed header reads."""

import json
import sqlite3
import time

from local_ai_router.savings import SavingsEngine, empty_total
from local_ai_router.store import Store


def test_header_reads_materialized_totals_at_two_million_rows(settings):
    store = Store(settings)
    try:
        savings = SavingsEngine(store)
        total = empty_total()
        total["runs"] = 100_000
        total["known"] = {"actual_cost": "20000", "baseline_estimated_cost": "100000", "estimated_cost_saved": "80000"}
        total["local_input_tokens"] = 2_000_000_000
        total["paid_cloud_tokens_avoided"] = 2_000_000_000
        with store.transaction():
            store.db.execute("""WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<100000)
                INSERT INTO savings_runs SELECT 'synthetic-'||i,'synthetic-'||i,'scale','2026-01-01','final','task',i,i,'{}','{}' FROM n""")
            store.db.execute("""WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<20)
                INSERT INTO usage_records SELECT r.request||'-'||n.i,r.request,'edit','executor',n.i,'{}' FROM savings_runs r CROSS JOIN n""")
            store.db.execute("INSERT INTO savings_totals VALUES('all','*',?)", (json.dumps(total),))
        assert store.db.execute("SELECT count(*) FROM savings_runs").fetchone()[0] == 100_000
        assert store.db.execute("SELECT count(*) FROM usage_records").fetchone()[0] == 2_000_000
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM savings_totals WHERE bucket='day' AND key>=? AND key<=?",
            ("2026-01-01", "2026-01-30"),
        ).fetchall()
        assert any("SEARCH" in row[3] for row in plan)
        reads = []

        def guard(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append(table)
                if table == "usage_records":
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.db.set_authorizer(guard)
        started = time.perf_counter()
        for period in ("today", "7d", "30d", "all"):
            result = savings.summary(period)
            if period == "all":
                assert result["runs"] == 100_000
                assert result["estimated_cost_saved"] == "80000"
        savings.state()
        assert time.perf_counter() - started < 3  # Generous bound; index-plan assertions are the main regression guard.
        assert "usage_records" not in reads
        store.db.set_authorizer(None)
    finally:
        store.close()
