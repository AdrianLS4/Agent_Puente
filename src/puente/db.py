from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT


def utcnow() -> datetime:
    """UTC naive (se guarda como ISO string)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def connect(path: str = ":memory:", check_same_thread: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(Path(ROOT / "schema.sql").read_text(encoding="utf-8"))
    for stmt in (
        "ALTER TABLE catalog_items ADD COLUMN billing_kind TEXT NOT NULL DEFAULT 'one_time'",
        "ALTER TABLE outbox ADD COLUMN template TEXT NOT NULL DEFAULT 'lock_request'",
        "ALTER TABLE ves_payments ADD COLUMN payer_bank TEXT",
        "ALTER TABLE ves_payments ADD COLUMN payer_phone TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    request_id: int | None,
    event_type: str,
    *,
    actor: str = "system",
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (request_id, ts, actor, event_type, from_state, to_state, payload)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            request_id,
            iso(now or utcnow()),
            actor,
            event_type,
            from_state,
            to_state,
            json.dumps(payload, default=str) if payload else None,
        ),
    )
