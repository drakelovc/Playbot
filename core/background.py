"""Фоновые задачи: вечный онлайн, автоподнятие, авто-восстановление, ежедневная сводка."""
from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timezone, timedelta

from playerokapi.enums import ItemStatuses

from core.config import load_config, save_config
import core.playerok_connection as conn

log = logging.getLogger("playerok_bot.background")


# --- Вечный онлайн ---
def permanent_online_loop():
    while True:
        cfg = load_config()
        if not cfg.get("permanent_online") or not conn.playerok_acc or not conn.playerok_status.get("connected"):
            time.sleep(60)
            continue
        try:
            conn.playerok_acc.get_chats(count=1)
        except Exception:
            pass
        time.sleep(120)


# --- Автоподнятие лотов ---
def _bump_local_now(cfg: dict) -> datetime:
    offset = int(cfg.get("bump_tz_offset", 3))
    return datetime.now(timezone(timedelta(hours=offset)))


def _is_bump_hot_hour(cfg: dict) -> bool:
    if not cfg.get("smart_bump_enabled"):
        return True
    ranges = cfg.get("smart_bump_ranges") or []
    if not ranges:
        return True
    now = _bump_local_now(cfg)
    weekday = now.isoweekday()
    hour = now.hour
    for r in ranges:
        days = r.get("days") or [1, 2, 3, 4, 5, 6, 7]
        if weekday not in days:
            continue
        h_from = int(r.get("from", 0)) % 24
        h_to = int(r.get("to", 0)) % 24
        if h_from == h_to:
            return True
        if h_from < h_to:
            if h_from <= hour < h_to:
                return True
        else:
            if hour >= h_from or hour < h_to:
                return True
    return False


def auto_bump_loop():
    while True:
        cfg = load_config()
        if not cfg.get("auto_bump") or not conn.playerok_acc or not conn.playerok_status.get("connected"):
            time.sleep(60)
            continue
        if not _is_bump_hot_hour(cfg):
            time.sleep(300)
            continue
        interval = cfg.get("auto_bump_interval", 30) * 60
        try:
            items = conn.playerok_acc.get_user_items(count=24)
            bumped = 0
            if items and hasattr(items, 'items'):
                for item in items.items:
                    try:
                        statuses = conn.playerok_acc.get_item_priority_statuses(item.id, item.price)
                        free = next((s for s in statuses if s.price == 0), None)
                        if free:
                            conn.playerok_acc.publish_item(item.id, free.id)
                            bumped += 1
                    except Exception:
                        pass
            if bumped > 0:
                log.info("[AUTO-BUMP] Поднято лотов: %d", bumped)
        except Exception as exc:
            log.error("Ошибка автоподнятия: %s", exc)
        time.sleep(interval)


# --- Авто-восстановление ---
_recently_restored_item_ids: dict[str, float] = {}


def auto_restore_loop():
    while True:
        cfg = load_config()
        if not cfg.get("auto_restore") or not conn.playerok_acc or not conn.playerok_status.get("connected"):
            time.sleep(60)
            continue
        interval = cfg.get("auto_restore_interval", 30) * 60
        try:
            now = time.time()
            expired_keys = [k for k, ts in _recently_restored_item_ids.items() if now - ts > interval]
            for k in expired_keys:
                _recently_restored_item_ids.pop(k, None)

            statuses_to_scan = [ItemStatuses.SOLD]
            if cfg.get("auto_restore_expired", True):
                statuses_to_scan.append(ItemStatuses.EXPIRED)

            items_result = conn.playerok_acc.get_my_items(statuses=statuses_to_scan, count=24)
            if items_result and hasattr(items_result, "items"):
                for item in items_result.items:
                    try:
                        item_id = item.id
                        if str(item_id) in _recently_restored_item_ids:
                            continue
                        item_price = getattr(item, "price", 0)
                        statuses = conn.playerok_acc.get_item_priority_statuses(item_id, item_price)
                        free_status = next((s for s in statuses if s.price == 0), None)
                        if free_status:
                            conn.playerok_acc.publish_item(item_id, free_status.id)
                    except Exception:
                        pass
        except Exception as exc:
            log.error("Ошибка авто-восстановления: %s", exc)
        time.sleep(interval)


# --- Ежедневная сводка ---
def daily_summary_loop():
    while True:
        try:
            cfg = load_config()
            if not cfg.get("daily_summary_enabled"):
                time.sleep(300)
                continue
            offset = int(cfg.get("bump_tz_offset", 3))
            now = datetime.now(timezone(timedelta(hours=offset)))
            target_hour = int(cfg.get("daily_summary_hour", 0))
            if now.hour == target_hour and now.minute < 5:
                from core.helpers import build_daily_summary
                text = build_daily_summary()
                try:
                    from core.bot_instance import bot, ADMIN_ID
                    bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
                except Exception:
                    pass
                time.sleep(3600)
            else:
                time.sleep(120)
        except Exception:
            time.sleep(300)


def start_background_threads():
    threading.Thread(target=permanent_online_loop, daemon=True).start()
    threading.Thread(target=auto_bump_loop, daemon=True).start()
    threading.Thread(target=auto_restore_loop, daemon=True).start()
    threading.Thread(target=daily_summary_loop, daemon=True).start()
