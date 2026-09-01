"""
history.py — durable record of every request the kernel handles.

The kernel previously kept nothing: a request was resolved, executed, and
forgotten. This stores each one so you can see what you asked, what the model
decided it meant, and what actually happened — which is also the only way to
notice the resolver quietly mis-routing something.

SQLite (stdlib) rather than a log file: the dashboard wants filtering,
pagination and counts, and sqlite3 handles concurrent writes from
ThreadingHTTPServer safely.

The DB lives outside the repo (it holds everything you've ever asked).
"""

import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.expanduser(os.environ.get("AGENT_HISTORY", "~/.coo/history.db"))

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    sender      TEXT,
    query       TEXT    NOT NULL,
    capability  TEXT,
    params      TEXT,
    status      TEXT    NOT NULL,
    result      TEXT,
    error       TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts DESC);
CREATE INDEX IF NOT EXISTS idx_requests_capability ON requests(capability);
"""


def _connect():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # WAL keeps a dashboard read from blocking a request write.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def record(query, status, sender=None, capability=None, params=None,
           result=None, error=None, duration_ms=None):
    """Never let bookkeeping break a request: swallow storage errors."""
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                "INSERT INTO requests (ts, sender, query, capability, params, "
                "status, result, error, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), sender, query, capability,
                 json.dumps(params) if params else None,
                 status, result, error, duration_ms))
            conn.commit()
    except Exception:
        pass


def recent(limit=100, offset=0, status=None, capability=None, search=None):
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if capability:
        where.append("capability = ?")
        args.append(capability)
    if search:
        where.append("(query LIKE ? OR result LIKE ? OR error LIKE ?)")
        args += [f"%{search}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        conn = _connect()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM requests {clause}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM requests {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
    return [dict(r) for r in rows], total


def seen_capabilities():
    """Capabilities that actually appear in history.

    The dashboard filter used to read the live registry, which coupled the
    always-on edge to capability metadata it otherwise never needs. This is also
    more truthful: it lists what has been used, including retired capabilities.
    """
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT DISTINCT capability FROM requests"
            " WHERE capability IS NOT NULL ORDER BY capability").fetchall()
    return [r["capability"] for r in rows]


def stats():
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(status = 'ok') AS ok,"
            " SUM(status = 'error') AS errors,"
            " SUM(status = 'confirmation_required') AS confirmations,"
            " SUM(status = 'no_action') AS no_action,"
            " SUM(status = 'unavailable') AS unavailable"
            " FROM requests").fetchone()
        caps = conn.execute(
            "SELECT capability, COUNT(*) AS n FROM requests"
            " WHERE capability IS NOT NULL"
            " GROUP BY capability ORDER BY n DESC").fetchall()
        # Median is more honest than a mean here: one cold model start would
        # drag an average badly.
        lat = conn.execute(
            "SELECT duration_ms FROM requests WHERE duration_ms IS NOT NULL"
            " ORDER BY duration_ms").fetchall()
    values = [r["duration_ms"] for r in lat]
    median = values[len(values) // 2] if values else None
    return {
        "total": row["total"] or 0,
        "ok": row["ok"] or 0,
        "errors": row["errors"] or 0,
        "confirmations": row["confirmations"] or 0,
        "no_action": row["no_action"] or 0,
        "unavailable": row["unavailable"] or 0,
        "median_ms": median,
        "capabilities": [{"capability": r["capability"], "count": r["n"]} for r in caps],
    }
