"""Persistence.

One SQLite file holds inventory, telemetry, alerts, incidents, jobs and an
audit trail. The audit table is the one that matters most in production: an
AIOps system that changes devices on its own must be able to answer "who
changed this port, when, and why" — and "who" includes the system itself
and the AI agent, each recorded as a distinct actor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    name TEXT PRIMARY KEY, vendor TEXT, role TEXT, mgmt TEXT, model TEXT,
    reachable INTEGER DEFAULT 1, cpu REAL, uptime INTEGER, last_seen REAL
);
CREATE TABLE IF NOT EXISTS interfaces (
    device TEXT, name TEXT, admin_up INTEGER, oper_up INTEGER, description TEXT,
    speed_mbps INTEGER, in_bps REAL, out_bps REAL, util REAL, err_per_min REAL,
    crc_total INTEGER, updated REAL, PRIMARY KEY (device, name)
);
CREATE TABLE IF NOT EXISTS links (
    a TEXT, a_if TEXT, b TEXT, b_if TEXT, discovered REAL, PRIMARY KEY (a, a_if)
);
CREATE TABLE IF NOT EXISTS metrics (
    ts REAL, device TEXT, metric TEXT, target TEXT, value REAL
);
CREATE INDEX IF NOT EXISTS metrics_lookup ON metrics (device, metric, target, ts);
CREATE TABLE IF NOT EXISTS events (
    ts REAL, device TEXT, severity INTEGER, kind TEXT, interface TEXT, state TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT, rule TEXT, severity TEXT,
    device TEXT, target TEXT, summary TEXT, status TEXT, started REAL, ended REAL,
    incident_id INTEGER, root INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (status, fingerprint);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, root_key TEXT, root_device TEXT, root_target TEXT,
    kind TEXT, confidence TEXT, status TEXT, opened REAL, closed REAL, impact TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, device TEXT, target TEXT,
    status TEXT, created REAL, finished REAL, requested_by TEXT, approved_by TEXT,
    plan TEXT, output TEXT, alert_id INTEGER
);
CREATE TABLE IF NOT EXISTS audit (
    ts REAL, actor TEXT, action TEXT, device TEXT, detail TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, tuple(params))

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, tuple(params)).fetchall()]

    def row(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        r = self.rows(sql, params)
        return r[0] if r else None

    def transaction(self):
        return _Tx(self)

    def audit(self, actor: str, action: str, device: str = "", detail: Any = "") -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        self.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (time.time(), actor, action, device, text))

    def prune(self, keep_seconds: float) -> None:
        cutoff = time.time() - keep_seconds
        self.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


class _Tx:
    # Collector writes for one poll cycle land atomically: a reader never sees
    # half the interfaces of a device updated and the rest stale.
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.db.execute("BEGIN")
        return self.store

    def __exit__(self, exc_type, *_):
        try:
            self.store.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()
