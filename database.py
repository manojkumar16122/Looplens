"""SQLite persistence layer for LoopLens.

Every function opens a short-lived connection, which keeps them safe to call
from FastAPI's threadpool. Multi-step writes run inside a single transaction.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from config import settings

SESSION_ACTIVE = "ACTIVE"
SESSION_PAUSED = "PAUSED"

CALL_ALLOWED = "allowed"
CALL_BLOCKED = "blocked"
CALL_CACHED = "cached"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    start_time   TEXT    NOT NULL,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL    NOT NULL DEFAULT 0.0,
    status       TEXT    NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name  TEXT    NOT NULL,
    arguments  TEXT    NOT NULL,
    tokens     INTEGER NOT NULL DEFAULT 0,
    status     TEXT    NOT NULL DEFAULT 'allowed',
    timestamp  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    diagnosis  TEXT NOT NULL,
    timestamp  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, id);
CREATE INDEX IF NOT EXISTS idx_loop_alerts_session ON loop_alerts(session_id, id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the database file and tables if they do not exist."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def create_session(session_id: str) -> dict[str, Any]:
    start = _utc_now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, start_time, status) VALUES (?, ?, ?)",
            (session_id, start, SESSION_ACTIVE),
        )
    return {"id": session_id, "start_time": start, "status": SESSION_ACTIVE,
            "total_tokens": 0, "total_cost": 0.0}


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_latest_session_id() -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM sessions ORDER BY rowid DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def set_session_status(session_id: str, status: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))


def record_call(
    session_id: str,
    tool_name: str,
    arguments_json: str,
    tokens: int,
    cost_delta: float,
    status: str,
) -> None:
    """Log a tool call and update session totals atomically."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tool_calls (session_id, tool_name, arguments, tokens, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, tool_name, arguments_json, tokens, status, _utc_now()),
        )
        conn.execute(
            "UPDATE sessions SET total_tokens = total_tokens + ?, total_cost = total_cost + ? "
            "WHERE id = ?",
            (tokens, cost_delta, session_id),
        )


def insert_alert(session_id: str, alert_type: str, diagnosis: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO loop_alerts (session_id, alert_type, diagnosis, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, alert_type, diagnosis, _utc_now()),
        )


def latest_alert_text(session_id: str, alert_type: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT diagnosis FROM loop_alerts WHERE session_id = ? AND alert_type = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, alert_type),
        ).fetchone()
    return row["diagnosis"] if row else None


def get_recent_calls(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent calls in chronological order."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tool_name, arguments, tokens, status, timestamp FROM tool_calls "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_token_series(session_id: str) -> list[int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tokens FROM tool_calls WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [row["tokens"] for row in rows]


def count_calls(session_id: str) -> tuple[int, int]:
    """Return (total_calls, blocked_calls) for a session."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked "
            "FROM tool_calls WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return int(row["total"]), int(row["blocked"])


def get_alerts(session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT alert_type, diagnosis, timestamp FROM loop_alerts "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def reset_db() -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM loop_alerts")
        conn.execute("DELETE FROM tool_calls")
        conn.execute("DELETE FROM sessions")
