"""Steam Guard: загрузка maFile, генерация кодов."""
from __future__ import annotations

import json
import os
import time
import logging

import steampy.guard as guard

from core.config import ACCOUNTS_FOLDER, CODE_PERIOD

log = logging.getLogger("playerok_bot.steam_guard")


def load_mafiles() -> dict[str, dict]:
    result = {}
    if not os.path.exists(ACCOUNTS_FOLDER):
        os.makedirs(ACCOUNTS_FOLDER)
    folders = [ACCOUNTS_FOLDER]
    for d in os.listdir(ACCOUNTS_FOLDER):
        p = os.path.join(ACCOUNTS_FOLDER, d)
        if os.path.isdir(p):
            folders.append(p)
    for folder in folders:
        for fn in os.listdir(folder):
            if not fn.endswith(".maFile"):
                continue
            path = os.path.join(folder, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("account_name", fn.replace(".maFile", ""))
                if data.get("shared_secret"):
                    result[name.lower()] = data
            except Exception:
                pass
    return result


def generate_code(account_name: str) -> str | None:
    mafiles = load_mafiles()
    data = mafiles.get(account_name.lower())
    if not data:
        return None
    try:
        return guard.generate_one_time_code(data["shared_secret"])
    except Exception as exc:
        log.error("Ошибка генерации кода %s: %s", account_name, exc)
        return None


def get_account_names() -> list[str]:
    return [d.get("account_name", k) for k, d in load_mafiles().items()]


def seconds_until_code_change() -> int:
    return CODE_PERIOD - (int(time.time()) % CODE_PERIOD)
