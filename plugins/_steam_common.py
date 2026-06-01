"""Общие утилиты для Steam-плагинов (`autosteamoffline`, `autosteamrental`).

В одном месте — то, что не должно дублироваться:
* загрузка/сохранение JSON,
* генерация Steam Guard кода через `steampy.guard`,
* парсинг .maFile (в т. ч. внутри ZIP),
* парсинг длительности аренды из текста (`на 2 часа`, `на 30 минут`...),
* генерация коротких ID для callback'ов Telegram,
* экранирование Markdown.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import string
import time
import zipfile
from typing import Any

try:
    import steampy.guard as _guard
except ImportError:  # pragma: no cover — steampy грузится в playerok_bot.py
    _guard = None  # type: ignore[assignment]


# ─── Файловые утилиты ────────────────────────────────────────────

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_json(path: str, default: Any) -> Any:
    # 1. Если включён SQLite-режим — пробуем сначала достать значение оттуда.
    if _sqlite_enabled():
        try:
            from . import _sqlite_store as _store
            val = _store.read(path)
            if val is not None:
                return val
            # SQLite пустой, но JSON-файл существует — выполняем
            # одноразовую миграцию (lazy migrate).
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _store.write(path, data)
                    return data
                except Exception:
                    return default
            return default
        except Exception:
            # Если SQLite-store не поднялся — откатываемся к JSON.
            pass

    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    if _sqlite_enabled():
        try:
            from . import _sqlite_store as _store
            _store.write(path, data)
            # При желании можно также писать JSON на диск как backup —
            # это удобно при отладке/инспекции, но дорого при больших объёмах.
            # Контролируется флагом PLAYEROK_SQLITE_BACKUP_JSON.
            if os.environ.get("PLAYEROK_SQLITE_BACKUP_JSON") == "1":
                pass  # fall through to JSON write
            else:
                return
        except Exception:
            pass

    ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _sqlite_enabled() -> bool:
    """Включён ли SQLite-бэкенд (env-flag `PLAYEROK_USE_SQLITE=1`)."""
    return os.environ.get("PLAYEROK_USE_SQLITE") == "1"


# ─── Время ───────────────────────────────────────────────────────

def now() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))


def seconds_until_code_change() -> int:
    return 30 - (int(time.time()) % 30)


# ─── Steam Guard ─────────────────────────────────────────────────

def generate_code(shared_secret: str) -> str | None:
    if not shared_secret or _guard is None:
        return None
    try:
        return _guard.generate_one_time_code(shared_secret)
    except Exception:
        return None


# ─── maFile парсинг ──────────────────────────────────────────────

_MAFILE_REQUIRED = ("shared_secret",)


def parse_mafile_bytes(raw: bytes) -> dict | None:
    """Парсит содержимое .maFile (bytes). Возвращает dict с ключами
    account_name, shared_secret, identity_secret, device_id, steam_id."""
    try:
        text = raw.decode("utf-8-sig", errors="replace").strip()
    except Exception:
        return None
    # .maFile может быть со «странным» хвостом — но валидный JSON в начале.
    try:
        data = json.loads(text)
    except Exception:
        # Попробуем найти первый "{...}" внутри
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in _MAFILE_REQUIRED):
        return None
    return {
        "account_name": data.get("account_name") or data.get("Session", {}).get("AccountName") or "",
        "shared_secret": data["shared_secret"],
        "identity_secret": data.get("identity_secret", ""),
        "device_id": data.get("device_id", ""),
        "steam_id": str(data.get("Session", {}).get("SteamID", "") or data.get("steam_id", "")),
    }


def parse_mafile_from_zip(raw: bytes) -> list[tuple[str, dict]]:
    """Возвращает список (filename, parsed_mafile) для всех валидных
    .maFile внутри ZIP-архива."""
    out: list[tuple[str, dict]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return out
    for name in zf.namelist():
        if not name.lower().endswith(".mafile"):
            continue
        try:
            data = parse_mafile_bytes(zf.read(name))
        except Exception:
            continue
        if data:
            out.append((os.path.basename(name), data))
    return out


# ─── Парсинг длительности из текста ──────────────────────────────

_DURATION_RE = re.compile(
    r"(\d+)\s*(сек(?:унд)?[а-я]*|мин(?:ут)?[а-я]*|час[а-я]*|сут[а-я]*|дн[а-я]+|ден[а-я]*|нед[а-я]+|мес[а-я]+)",
    re.IGNORECASE,
)


def parse_duration_minutes(text: str) -> int | None:
    """Ищет в тексте паттерн вида `на 2 часа`, `30 минут`, `1 сутки` и т. п.
    Возвращает длительность в минутах или None."""
    if not text:
        return None
    m = _DURATION_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("сек"):
        return max(1, n // 60) if n >= 60 else 1
    if unit.startswith("мин"):
        return n
    if unit.startswith("час"):
        return n * 60
    if unit.startswith("сут") or unit.startswith("дн") or unit.startswith("ден"):
        return n * 24 * 60
    if unit.startswith("нед"):
        return n * 7 * 24 * 60
    if unit.startswith("мес"):
        return n * 30 * 24 * 60
    return None


def human_minutes(m: int) -> str:
    """`120` → `2 ч`, `45` → `45 мин`, `1500` → `1 д 1 ч`."""
    if m <= 0:
        return "—"
    days, r = divmod(m, 24 * 60)
    hours, mins = divmod(r, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if mins and not days:
        parts.append(f"{mins} мин")
    return " ".join(parts) or f"{m} мин"


def human_seconds(s: int) -> str:
    if s <= 0:
        return "0 сек"
    if s < 60:
        return f"{s} сек"
    return human_minutes(s // 60)


# ─── Генерация ───────────────────────────────────────────────────

def gen_password(length: int = 14) -> str:
    # Без `Il10O0` чтобы покупатель не путал.
    alphabet = (string.ascii_letters + string.digits).translate(
        str.maketrans("", "", "Il1O0o")
    )
    return "".join(random.choices(alphabet, k=length))


def short_id(s: str) -> str:
    """Короткий 8-символьный детерминированный ID для Telegram callback_data
    (там лимит 64 байта). Используем sha1, чтобы из длинных alias'ов / itemId
    получать стабильный короткий ключ."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ─── Экранирование Markdown ──────────────────────────────────────

_MD_ESC = "_*[]()~`>#+-=|{}.!"


def md_escape(s: str) -> str:
    if not s:
        return ""
    out = []
    for ch in s:
        if ch in _MD_ESC:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def fmt_template(tpl: str, **kwargs: Any) -> str:
    """Простая подстановка `{key}` → значение. Если ключа нет — оставляем
    плейсхолдер как есть (это удобно при опечатках в шаблонах)."""
    for k, v in kwargs.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl
