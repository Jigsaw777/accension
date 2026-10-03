"""SQLite WAL cache, traces, budget reservations and gradual quality estimates."""
from __future__ import annotations
import hashlib, json, sqlite3, threading, time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from .config import Settings

class BudgetExceeded(RuntimeError):
    pass

def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()

class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = sqlite3.connect(settings.state / "router.sqlite3", timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.hot: OrderedDict = OrderedDict()
        self.hits = self.misses = 0
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=10000;
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS traces(seq INTEGER PRIMARY KEY, request TEXT, stage TEXT, stamp REAL, data TEXT);
        CREATE INDEX IF NOT EXISTS traces_request ON traces(request);
        CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, request TEXT, session TEXT, day TEXT, role TEXT,
          model TEXT, frontier INTEGER, cost REAL, state TEXT, usage TEXT, latency REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS profiles(model TEXT, family TEXT, n INTEGER, success REAL, latency REAL,
          PRIMARY KEY(model,family));
        CREATE TABLE IF NOT EXISTS health(model TEXT PRIMARY KEY, failures INTEGER, until REAL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, repo TEXT, fingerprint TEXT, status TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()

    def metadata(self, key, value=None):
        with self.lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def get(self, key: str):
        if not self.settings.cache.enabled:
            return None
        now = time.time()
        with self.lock:
            if key in self.hot:
                value, expires = self.hot.pop(key)
                if expires > now:
                    self.hot[key] = (value, expires)
                    self.hits += 1
                    return json.loads(value)
            row = self.db.execute("SELECT value, expires FROM cache WHERE key=? AND expires>?", (key, now)).fetchone()
            if row:
                self._hot(key, row[0], row[1])
                self.hits += 1
                return json.loads(row[0])
            self.misses += 1
            return None

    def _hot(self, key, value, expires):
        self.hot[key] = (value, expires)
        while len(self.hot) > self.settings.cache.lru_entries:
            self.hot.popitem(last=False)

    def put(self, key, value, ttl=None):
        if not self.settings.cache.enabled:
            return
        payload = json.dumps(value, default=str)
        expiry = time.time() + (ttl or self.settings.cache.ttl_seconds)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, payload, expiry))
            self.db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))
            self.db.execute("DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires DESC LIMIT -1 OFFSET ?)", (self.settings.cache.max_entries,))
            self._hot(key, payload, expiry)

    def cache_stats(self):
        return {"entries": self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], "process_hits": self.hits, "process_misses": self.misses}

    def clear_cache(self):
        with self.lock:
            self.hot.clear()
            self.db.execute("DELETE FROM cache")

    def trace(self, request: str, stage: str, **data):
        # Call sites provide metadata only. Defense in depth removes secret-like keys/values.
        from .safety import redact
        with self.lock:
            self.db.execute("INSERT INTO traces(request,stage,stamp,data) VALUES(?,?,?,?)", (request, stage, time.time(), json.dumps(redact(data), default=str)))

    def traces(self, request: str):
        return [{"stage": r["stage"], "timestamp": r["stamp"], **json.loads(r["data"])} for r in self.db.execute("SELECT * FROM traces WHERE request=? ORDER BY seq", (request,))]

    def reserve(self, request, session, role, model, estimate, limit):
        b = self.settings.budgets
        day = datetime.now(timezone.utc).date().isoformat()
        if estimate < 0 or not __import__('math').isfinite(estimate):
            raise BudgetExceeded("Unknown or invalid price")
        with self.transaction():
            def total(clause, values):
                return self.db.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE " + clause, values).fetchone()[0]
            checks = [(total("request=?", (request,)), limit, "request"),
                      (total("session=?", (session,)), b.session_budget, "session"),
                      (total("day=?", (day,)), b.daily_hard_budget, "daily")]
            bucket = "planner" if role == "planner" else "verification" if role in ("reviewer", "arbiter") else "worker"
            roles = ("planner",) if bucket == "planner" else ("reviewer", "arbiter") if bucket == "verification" else ("executor", "repair", "gateway")
            spent = total("request=? AND role IN (" + ",".join("?" for _ in roles) + ")", (request, *roles))
            checks.append((spent, getattr(b, bucket + "_budget"), bucket))
            for used, cap, name in checks:
                if used + estimate > cap + 1e-10:
                    raise BudgetExceeded(f"{name} budget exceeded")
            if model.tier == 4:
                count = self.db.execute("SELECT COUNT(*) FROM calls WHERE request=? AND frontier=1", (request,)).fetchone()[0]
                plans = self.db.execute("SELECT COUNT(*) FROM calls WHERE request=? AND frontier=1 AND role='planner'", (request,)).fetchone()[0]
                if count >= b.max_frontier_calls or (role == "planner" and plans >= b.max_initial_frontier_plans):
                    raise BudgetExceeded("Frontier call limit reached")
            call = uuid4().hex
            self.db.execute("INSERT INTO calls(id,request,session,day,role,model,frontier,cost,state,usage) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (call, request, session, day, role, model.id, int(model.tier == 4), estimate, "reserved", "{}"))
        return call

    def settle(self, call, cost, usage, latency, failed=False):
        # Uncertain/failed calls retain their reservation; a timeout may still be billable.
        with self.lock:
            self.db.execute("UPDATE calls SET cost=COALESCE(?,cost), state=?, usage=?, latency=? WHERE id=?",
                            (cost, "uncertain" if failed else "complete", json.dumps(usage), latency, call))

    def costs(self, request=None):
        rows = list(self.db.execute("SELECT * FROM calls" + (" WHERE request=?" if request else ""), (request,) if request else ()))
        day = datetime.now(timezone.utc).date().isoformat()
        today = sum(r["cost"] for r in rows if r["day"] == day)
        return {"estimated_usd": round(sum(r["cost"] for r in rows), 8), "today_usd": round(today, 8), "calls": len(rows),
                "frontier_calls": sum(r["frontier"] for r in rows), "uncertain_calls": sum(r["state"] != "complete" for r in rows),
                "soft_budget_exceeded": today >= self.settings.budgets.daily_soft_budget,
                "by_model": {m: round(sum(r["cost"] for r in rows if r["model"] == m), 8) for m in {r["model"] for r in rows}}}

    def success(self, model, family, passed, latency):
        with self.transaction():
            r = self.db.execute("SELECT * FROM profiles WHERE model=? AND family=?", (model, family)).fetchone()
            n, old, old_latency = (r["n"], r["success"], r["latency"]) if r else (0, .5, latency)
            self.db.execute("INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?)", (model, family, n+1, .1*int(passed)+.9*old, .1*latency+.9*old_latency))

    def quality(self, model, family):
        prior = model.quality_priors.get(family, model.quality_priors.get("default", .8))
        row = self.db.execute("SELECT * FROM profiles WHERE model=? AND family=?", (model.id, family)).fetchone()
        if not row:
            return prior
        weight = min(.5, row["n"] / 100)
        return prior * (1-weight) + row["success"] * weight

    def healthy(self, model):
        row = self.db.execute("SELECT until FROM health WHERE model=?", (model,)).fetchone()
        return not row or row[0] <= time.time()

    def health_result(self, model, ok):
        with self.transaction():
            row = self.db.execute("SELECT failures FROM health WHERE model=?", (model,)).fetchone()
            failures = 0 if ok else (row[0] if row else 0) + 1
            until = time.time()+self.settings.routing.circuit_cooldown if failures >= self.settings.routing.circuit_failures else 0
            self.db.execute("INSERT OR REPLACE INTO health VALUES(?,?,?)", (model, failures, until))
