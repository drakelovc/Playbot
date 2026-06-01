"""Подключение к Playerok API и состояние соединения."""
from __future__ import annotations

import logging
import traceback
from datetime import datetime

from playerokapi.account import Account as PlayerokAccount
from playerokapi.listener.listener import EventListener

log = logging.getLogger("playerok_bot.connection")

playerok_acc: PlayerokAccount | None = None
playerok_listener: EventListener | None = None
playerok_running = False
playerok_status: dict = {
    "connected": False,
    "last_sync": None,
    "error": None,
    "username": None,
    "email": None,
    "balance": None,
    "rating": None,
    "reviews_count": 0,
    "items_total": 0,
    "deals_incoming_total": 0,
    "deals_incoming_finished": 0,
}


def init_playerok(cfg: dict) -> bool:
    global playerok_acc, playerok_listener, playerok_status
    cookies = cfg.get("playerok_cookies", "")
    user_agent = cfg.get("playerok_user_agent", "")
    proxy_raw = cfg.get("playerok_proxy", "") or ""
    proxy = proxy_raw.replace("https://", "").replace("http://", "").strip() or None

    if not cookies:
        playerok_status = {**playerok_status, "connected": False, "error": "Куки не заданы"}
        return False

    try:
        if hasattr(PlayerokAccount, "instance"):
            delattr(PlayerokAccount, "instance")

        playerok_acc = PlayerokAccount(
            cookies=cookies,
            user_agent=user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            proxy=proxy,
        ).get()

        playerok_listener = EventListener(playerok_acc)

        balance_val = None
        rating_val = None
        reviews = 0
        items_total = 0
        deals_in_total = 0
        deals_in_finished = 0
        try:
            if hasattr(playerok_acc, 'balance') and playerok_acc.balance:
                balance_val = (
                    playerok_acc.balance.value / 100
                    if hasattr(playerok_acc.balance, 'value') else None
                )
            if hasattr(playerok_acc, 'stats') and playerok_acc.stats:
                if hasattr(playerok_acc.stats, 'items'):
                    items_total = playerok_acc.stats.items.total
                if hasattr(playerok_acc.stats, 'deals') and playerok_acc.stats.deals:
                    if hasattr(playerok_acc.stats.deals, 'incoming'):
                        deals_in_total = playerok_acc.stats.deals.incoming.total
                        deals_in_finished = playerok_acc.stats.deals.incoming.finished
            if hasattr(playerok_acc, 'rating'):
                rating_val = playerok_acc.rating
            if hasattr(playerok_acc, 'reviews_count'):
                reviews = playerok_acc.reviews_count
        except Exception:
            pass

        playerok_status = {
            "connected": True,
            "last_sync": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "error": None,
            "username": playerok_acc.username,
            "email": getattr(playerok_acc, 'email', None),
            "balance": balance_val,
            "rating": rating_val,
            "reviews_count": reviews,
            "items_total": items_total,
            "deals_incoming_total": deals_in_total,
            "deals_incoming_finished": deals_in_finished,
        }
        log.info("Playerok подключён: %s", playerok_acc.username)
        return True
    except Exception as exc:
        playerok_status = {**playerok_status, "connected": False, "error": str(exc)}
        log.error("Ошибка подключения Playerok: %s\n%s", exc, traceback.format_exc())
        return False
