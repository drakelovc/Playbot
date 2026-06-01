"""Postgres-бэкап файлов бота для Railway.

Зачем: на Railway локальная файловая система **эфемерна** — при каждом
редеплое содержимое `accounts/` и JSON-конфигов стирается, потому что
контейнер пересоздаётся. Этот модуль прозрачно зеркалит выбранные
файлы/папки в Postgres, чтобы данные переживали редеплои.

Использование:
    import db
    db.init()                                              # на старте
    db.hydrate(["playerok_config.json", "accounts"])       # восстановить файлы из БД
    db.start_sync_loop(["playerok_config.json", "accounts"])  # фоновая синхронизация
    db.save_file("playerok_config.json")                   # моментальный push (опционально)

Если `DATABASE_URL` не задан или `psycopg2` недоступен — модуль становится
no-op'ом, бот продолжает работать на одних только локальных файлах.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Iterable

log = logging.getLogger("db")

try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    _DRIVER_OK = True
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    ThreadedConnectionPool = None  # type: ignore[assignment]
    _DRIVER_OK = False

DATABASE_URL = os.getenv("DATABASE_URL", "")
try:
    SYNC_INTERVAL_SEC = max(5, int(os.getenv("DB_SYNC_INTERVAL", "30")))
except ValueError:
    SYNC_INTERVAL_SEC = 30

_pool = None
_enabled = False
_sync_thread: threading.Thread | None = None
_sync_stop = threading.Event()


def _normalize_url(url: str) -> str:
    # SQLAlchemy-style postgres:// → psycopg2-friendly postgresql://
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def init() -> bool:
    """Подключается к БД, создаёт таблицу `bot_files`. Возвращает True если БД активна."""
    global _pool, _enabled
    if _enabled:
        return True
    if not DATABASE_URL:
        log.info("DATABASE_URL не задан — Postgres-бэкап выключен.")
        return False
    if not _DRIVER_OK:
        log.warning("psycopg2 не установлен — добавь `psycopg2-binary` в requirements.txt.")
        return False
    try:
        url = _normalize_url(DATABASE_URL)
        _pool = ThreadedConnectionPool(1, 5, url, connect_timeout=10)
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS bot_files (
                        path TEXT PRIMARY KEY,
                        content BYTEA NOT NULL,
                        sha256 TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )"""
                )
            conn.commit()
        finally:
            _pool.putconn(conn)
        _enabled = True
        log.info("Postgres-бэкап подключён.")
        return True
    except Exception as e:
        log.exception("Не удалось подключиться к Postgres: %s", e)
        _pool = None
        _enabled = False
        return False


def is_enabled() -> bool:
    return _enabled


def _conn():
    if not _enabled or _pool is None:
        return None
    return _pool.getconn()


def _release(conn) -> None:
    if conn is not None and _pool is not None:
        try:
            _pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _matches_root(path: str, root: str) -> bool:
    p = _norm(path)
    r = _norm(root).rstrip("/")
    return p == r or p.startswith(r + "/")


def save_file(path: str) -> None:
    """Заливает один локальный файл в БД (upsert по пути)."""
    if not _enabled:
        return
    if not os.path.isfile(path):
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        log.warning("save_file: не прочесть %s: %s", path, e)
        return
    h = _sha256(data)
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO bot_files (path, content, sha256, updated_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (path) DO UPDATE SET
                     content = EXCLUDED.content,
                     sha256 = EXCLUDED.sha256,
                     updated_at = NOW()
                   WHERE bot_files.sha256 IS DISTINCT FROM EXCLUDED.sha256""",
                (_norm(path), psycopg2.Binary(data), h),
            )
        conn.commit()
    except Exception as e:
        log.warning("save_file: ошибка БД для %s: %s", path, e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _release(conn)


def delete_file(path: str) -> None:
    """Удаляет файл из БД."""
    if not _enabled:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_files WHERE path = %s", (_norm(path),))
        conn.commit()
    except Exception as e:
        log.warning("delete_file: ошибка БД для %s: %s", path, e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _release(conn)


def hydrate(roots: Iterable[str]) -> int:
    """Восстанавливает файлы из БД на диск.

    Файл восстанавливается только если на диске его ещё нет (или он пустой) —
    это безопасно: если бот успел что-то записать локально до hydrate, не
    перетрём.
    """
    if not _enabled:
        return 0
    roots = list(roots)
    conn = _conn()
    if conn is None:
        return 0
    rows: list[tuple[str, memoryview]] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT path, content FROM bot_files")
            rows = cur.fetchall()
    except Exception as e:
        log.warning("hydrate: ошибка БД: %s", e)
        _release(conn)
        return 0
    finally:
        _release(conn)

    restored = 0
    for path, content in rows:
        if not any(_matches_root(path, r) for r in roots):
            continue
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                continue
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "wb") as f:
                f.write(bytes(content))
            restored += 1
        except Exception as e:
            log.warning("hydrate: не записать %s: %s", path, e)
    if restored:
        log.info("Из Postgres восстановлено файлов: %d", restored)
    return restored


def _list_disk(roots: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for r in roots:
        if os.path.isfile(r):
            found.add(_norm(r))
        elif os.path.isdir(r):
            for root, _, files in os.walk(r):
                for fn in files:
                    found.add(_norm(os.path.join(root, fn)))
    return found


def _list_db_paths(roots: Iterable[str]) -> set[str]:
    if not _enabled:
        return set()
    conn = _conn()
    if conn is None:
        return set()
    paths: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT path FROM bot_files")
            for (p,) in cur.fetchall():
                if any(_matches_root(p, r) for r in roots):
                    paths.add(p)
    except Exception as e:
        log.warning("list_db_paths: %s", e)
    finally:
        _release(conn)
    return paths


def sync_now(roots: Iterable[str]) -> tuple[int, int]:
    """Двухсторонняя синхронизация: текущие файлы → БД, удалённые → удаляем из БД.

    Возвращает (uploaded, removed). `uploaded` считает все upsert-вызовы;
    реальные апдейты в БД произойдут только при изменении содержимого.
    """
    if not _enabled:
        return 0, 0
    roots = list(roots)
    on_disk = _list_disk(roots)
    in_db = _list_db_paths(roots)

    uploaded = 0
    for p in on_disk:
        save_file(p)
        uploaded += 1

    removed = 0
    for p in in_db - on_disk:
        delete_file(p)
        removed += 1

    return uploaded, removed


def start_sync_loop(roots: Iterable[str], interval: int | None = None) -> None:
    """Запускает фоновый поток с периодической синхронизацией."""
    global _sync_thread
    if not _enabled:
        return
    if _sync_thread and _sync_thread.is_alive():
        return
    roots = list(roots)
    iv = interval or SYNC_INTERVAL_SEC

    def _loop():
        while not _sync_stop.is_set():
            try:
                sync_now(roots)
            except Exception as e:
                log.exception("sync_loop: %s", e)
            _sync_stop.wait(iv)

    _sync_thread = threading.Thread(target=_loop, name="db-sync", daemon=True)
    _sync_thread.start()
    log.info("Запущен фоновый Postgres-sync, интервал %s сек.", iv)


def stop_sync_loop() -> None:
    _sync_stop.set()
