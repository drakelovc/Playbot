"""Вспомогательные функции: daily summary, аналитика, фильтрация истории."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from core.config import load_config


def normalize_proxy(raw: str) -> str:
    """Приводит прокси к формату http://login:pass@ip:port.

    Поддерживает входные форматы:
      - ip:port:login:pass
      - login:pass@ip:port
      - http://login:pass@ip:port  (уже правильный)
      - http://ip:port  (без авторизации)
      - ip:port  (без авторизации)
    """
    s = raw.strip()
    if not s:
        return ""

    # Убираем схему для парсинга
    scheme = ""
    for prefix in ("https://", "http://", "socks5://", "socks4://"):
        if s.lower().startswith(prefix):
            scheme = prefix
            s = s[len(prefix):]
            break

    # Формат login:pass@ip:port — уже хороший
    if "@" in s:
        if not scheme:
            scheme = "http://"
        return f"{scheme}{s}"

    parts = s.split(":")
    if len(parts) == 4:
        # ip:port:login:pass
        ip, port, login, password = parts
        if not scheme:
            scheme = "http://"
        return f"{scheme}{login}:{password}@{ip}:{port}"
    elif len(parts) == 2:
        # ip:port (без авторизации)
        if not scheme:
            scheme = "http://"
        return f"{scheme}{s}"

    # Не удалось распознать — возвращаем с http:// если нет схемы
    if not scheme:
        scheme = "http://"
    return f"{scheme}{s}"


def filter_history_by_period(history: list, period: str) -> list:
    now = datetime.now()
    if period == "24h":
        cutoff = now - timedelta(hours=24)
    elif period == "7d":
        cutoff = now - timedelta(days=7)
    elif period == "30d":
        cutoff = now - timedelta(days=30)
    else:
        return history
    result = []
    for h in history:
        t = h.get("time", "")
        try:
            dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
            if dt >= cutoff:
                result.append(h)
        except Exception:
            pass
    return result


def build_daily_summary() -> str:
    cfg = load_config()
    stats = cfg.get("stats", {})
    history = cfg.get("history", [])
    today = filter_history_by_period(history, "24h")

    deals_today = sum(1 for h in today if h.get("type") == "deal")
    codes_today = sum(1 for h in today if h.get("type") == "code")
    revenue_today = sum(h.get("price", 0) for h in today if h.get("type") == "deal")

    lines = [
        "📊 *Ежедневная сводка*",
        "",
        f"🛒 Сделок за 24ч: {deals_today}",
        f"💵 Выручка за 24ч: {revenue_today:.2f} ₽",
        f"🔑 Кодов за 24ч: {codes_today}",
        "",
        "📈 *Всего:*",
        f"🛒 Сделок: {stats.get('deals_total', 0)}",
        f"💵 Выручка: {stats.get('revenue', 0):.2f} ₽",
        f"🔑 Кодов: {stats.get('codes_sent', 0)}",
        f"💬 Сообщений: {stats.get('messages_received', 0)}",
    ]
    return "\n".join(lines)
