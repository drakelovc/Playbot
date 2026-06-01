"""SQLite-бэкенд для хранилищ плагинов.

Прозрачная замена JSON-файлов на одну SQLite-БД `storage/plugins.db`,
с lazy-миграцией из существующих JSON. Включается флагом окружения
`PLAYEROK_USE_SQLITE=1`.

Схема:
    blobs(path TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)

Где `path` — это полный относительный путь JSON-файла (например,
`storage/plugins/autosteamrental/accounts.json`), а `value` — сериализованный
JSON. Атомарность обеспечивается WAL-режимом + транзакциями SQLite.

Преимущества vs JSON-файлы:
- одно соединение, одна `fsync` на коммит;
- не появляются `.tmp`-файлы при сбое во время `os.replace`;
- работа при 1000+ записей не деградирует.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

_DB_PATH = os.path.join("storage", "plugins.db")
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS blobs ("
        "path TEXT PRIMARY KEY, "
        "value TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL"
        ")"
    )
    conn.commit()
    _conn = conn
    return conn


def read(path: str) -> Any | None:
    """Достаёт значение по path. None — если такой записи нет."""
    with _lock:
        cur = _connect().execute(
            "SELECT value FROM blobs WHERE path = ?", (path,))
        row = cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None


def write(path: str, data: Any) -> None:
    """Атомарно записывает значение под ключом path."""
    payload = json.dumps(data, ensure_ascii=False)
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO blobs(path, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (path, payload, int(time.time())),
        )
        conn.commit()


def delete(path: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM blobs WHERE path = ?", (path,))
        conn.commit()


def list_keys(prefix: str = "") -> list[str]:
    with _lock:
        cur = _connect().execute(
            "SELECT path FROM blobs WHERE path LIKE ? ORDER BY path",
            (prefix + "%",))
        return [r[0] for r in cur.fetchall()]


def migrate_dir(directory: str) -> int:
    """Прогоняет все JSON-файлы в `directory` (рекурсивно) и переносит их
    в SQLite. Возвращает число мигрированных файлов. Файлы остаются на
    диске — это страховка на случай отката."""
    count = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if not name.endswith(".json"):
                continue
            full = os.path.join(root, name)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            write(full, data)
            count += 1
    return count


def stats() -> dict[str, Any]:
    with _lock:
        cur = _connect().execute("SELECT COUNT(*), MAX(updated_at) FROM blobs")
        count, last_ts = cur.fetchone()
        try:
            size_bytes = os.path.getsize(_DB_PATH)
        except OSError:
            size_bytes = 0
        return {"rows": int(count or 0),
                "last_updated_at": int(last_ts or 0),
                "size_bytes": size_bytes,
                "db_path": _DB_PATH}
