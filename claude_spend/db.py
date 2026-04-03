from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

APP_DIR = Path.home() / ".claude-spend"
DB_PATH = APP_DIR / "claude_spend.db"
DEFAULT_TTL_SECONDS = 300


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    ensure_app_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    ensure_app_dir()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_cache (
                id INTEGER PRIMARY KEY,
                cache_key TEXT UNIQUE,
                response_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def get_cached(cache_key: str) -> Optional[Any]:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT response_json, expires_at FROM usage_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()

    if not row:
        return None

    if _from_iso(row["expires_at"]) <= _utc_now():
        delete_cache_key(cache_key)
        return None

    return json.loads(row["response_json"])


def set_cache(cache_key: str, payload: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    init_db()
    now = _utc_now()
    expires_at = now + timedelta(seconds=ttl_seconds)

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO usage_cache (cache_key, response_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response_json = excluded.response_json,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (cache_key, json.dumps(payload), _to_iso(now), _to_iso(expires_at)),
        )
        conn.commit()


def delete_cache_key(cache_key: str) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM usage_cache WHERE cache_key = ?", (cache_key,))
        conn.commit()


def clear_cache() -> None:
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM usage_cache")
        conn.commit()


def purge_expired_cache() -> int:
    init_db()
    now_iso = _to_iso(_utc_now())
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM usage_cache WHERE expires_at <= ?", (now_iso,))
        conn.commit()
        return cursor.rowcount
