"""Плагин autoconfirm -- автоподтверждение сделок с задержкой.

Позволяет:
  * Автоматически подтверждать сделки через N минут после оплаты.
  * Управлять задержкой и отменять запланированные подтверждения.

Telegram-команда -- /autoconfirm.
Хранилище: storage/plugins/autoconfirm/{config,pending}.json.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.autoconfirm")
STORAGE_DIR = os.path.join("storage", "plugins", "autoconfirm")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
PENDING_FILE = os.path.join(STORAGE_DIR, "pending.json")

# Lock protecting concurrent read-modify-write on pending.json
_pending_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "delay_minutes": 15,
    "notify_on_confirm": True,
}


# --- Config helpers --------------------------------------------------------

def get_config() -> dict[str, Any]:
    cfg = common.load_json(CONFIG_FILE, None)
    changed = False
    if cfg is None:
        cfg = dict(DEFAULT_CONFIG)
        changed = True
    else:
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
    if changed:
        common.save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    common.save_json(CONFIG_FILE, cfg)


# --- Pending helpers -------------------------------------------------------

def load_pending() -> list[dict[str, Any]]:
    return common.load_json(PENDING_FILE, [])


def save_pending(pending: list[dict[str, Any]]) -> None:
    common.save_json(PENDING_FILE, pending)


def add_pending(deal_id: str, delay_minutes: int) -> None:
    with _pending_lock:
        pending = load_pending()
        # Avoid duplicates
        for p in pending:
            if p.get("deal_id") == deal_id:
                return
        pending.append({
            "deal_id": deal_id,
            "scheduled_at": common.now(),
            "confirm_after_minutes": delay_minutes,
        })
        save_pending(pending)


def remove_pending(deal_id: str) -> bool:
    with _pending_lock:
        pending = load_pending()
        new_pending = [p for p in pending if p.get("deal_id") != deal_id]
        if len(new_pending) < len(pending):
            save_pending(new_pending)
            return True
        return False


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="autoconfirm",
    name="\u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435+",
    icon="\u23f2\ufe0f",
    description=(
        "\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u043e\u0435 "
        "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 "
        "\u0441\u0434\u0435\u043b\u043e\u043a \u0447\u0435\u0440\u0435\u0437 N \u043c\u0438\u043d\u0443\u0442. "
        "/autoconfirm \u0434\u043b\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438."
    ),
    instruction=(
        "*\u23f2\ufe0f \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435+*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u041f\u043e\u0441\u043b\u0435 \u043e\u043f\u043b\u0430\u0442\u044b \u0441\u0434\u0435\u043b\u043a\u0438 "
        "\u043f\u043b\u0430\u043d\u0438\u0440\u0443\u0435\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 "
        "\u0447\u0435\u0440\u0435\u0437 N \u043c\u0438\u043d\u0443\u0442.\n"
        "- \u041c\u043e\u0436\u043d\u043e \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c "
        "\u0437\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435.\n\n"
        "*\u041a\u0430\u043a \u043d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c:*\n"
        "1. `/autoconfirm` - \u043e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c.\n"
        "2. \u0423\u043a\u0430\u0437\u0430\u0442\u044c \u0437\u0430\u0434\u0435\u0440\u0436\u043a\u0443 \u0432 \u043c\u0438\u043d\u0443\u0442\u0430\u0445.\n"
        "3. \u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u043b\u0430\u0433\u0438\u043d."
    ),
    default_enabled=True,
    keywords=("\u0430\u0432\u0442\u043e\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435", "autoconfirm"),
)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Handler for the autoconfirm plugin."""

    _bg_thread: threading.Thread | None = None
    _bg_stop: bool = False

    def setup(self, ctx: PluginContext) -> None:
        get_config()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["autoconfirm"])
        def cmd_autoconfirm(message):
            if message.from_user.id != admin_id:
                return
            _send_main_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ac:"))
        def on_callback(call):
            if call.from_user.id != admin_id:
                return
            data = call.data
            chat_id = call.message.chat.id
            msg_id = call.message.message_id
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""

            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass

            if action == "main":
                _send_main_panel(bot, chat_id, msg_id)
            elif action == "toggle":
                cfg = get_config()
                cfg["enabled"] = not cfg.get("enabled", False)
                save_config(cfg)
                _send_main_panel(bot, chat_id, msg_id)
            elif action == "set_delay":
                wait_state[chat_id] = {"step": "wait_delay"}
                bot.send_message(
                    chat_id,
                    "\u23f2\ufe0f \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u0430\u0434\u0435\u0440\u0436\u043a\u0443 "
                    "\u0432 \u043c\u0438\u043d\u0443\u0442\u0430\u0445 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: 15):"
                )
            elif action == "pending":
                _send_pending(bot, chat_id, msg_id)
            elif action == "cancel" and len(parts) >= 3:
                deal_id = parts[2]
                if remove_pending(deal_id):
                    bot.send_message(chat_id, "\u2705 \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.")
                else:
                    bot.send_message(chat_id, "\u274c \u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e.")
                _send_pending(bot, chat_id)

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
            content_types=["text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id, {})
            step = state.get("step")
            text = (message.text or "").strip()

            if step == "wait_delay":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "\u041d\u0443\u0436\u043d\u043e \u0446\u0435\u043b\u043e\u0435 "
                                     "\u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0435 \u0447\u0438\u0441\u043b\u043e.")
                    return
                cfg = get_config()
                cfg["delay_minutes"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 \u0417\u0430\u0434\u0435\u0440\u0436\u043a\u0430: {val} \u043c\u0438\u043d.")
                _send_main_panel(bot, message.chat.id)

        def _send_main_panel(b, chat_id, edit_msg_id=None):
            cfg = get_config()
            enabled = cfg.get("enabled", False)
            delay = cfg.get("delay_minutes", 15)
            status_icon = "\u2705" if enabled else "\u274c"
            status_text = "\u0412\u043a\u043b\u044e\u0447\u0435\u043d" if enabled else "\u0412\u044b\u043a\u043b\u044e\u0447\u0435\u043d"
            pending = load_pending()
            text = (
                "\u23f2\ufe0f *\u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435+*\n\n"
                f"\u0421\u0442\u0430\u0442\u0443\u0441: {status_icon} {status_text}\n"
                f"\u0417\u0430\u0434\u0435\u0440\u0436\u043a\u0430: {delay} \u043c\u0438\u043d.\n"
                f"\u0412 \u043e\u0447\u0435\u0440\u0435\u0434\u0438: {len(pending)}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            toggle_text = "\u274c \u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c" if enabled else "\u2705 \u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c"
            kb.row(tg_types.InlineKeyboardButton(toggle_text, callback_data="ac:toggle"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u23f2\ufe0f \u0417\u0430\u0434\u0435\u0440\u0436\u043a\u0430", callback_data="ac:set_delay"))
            kb.row(tg_types.InlineKeyboardButton(
                f"\U0001f4cb \u041e\u0447\u0435\u0440\u0435\u0434\u044c ({len(pending)})",
                callback_data="ac:pending"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_pending(b, chat_id, edit_msg_id=None):
            pending = load_pending()
            if not pending:
                text = "\u23f2\ufe0f *\u041e\u0447\u0435\u0440\u0435\u0434\u044c \u043f\u0443\u0441\u0442\u0430.*"
            else:
                lines = ["\u23f2\ufe0f *\u041e\u0436\u0438\u0434\u0430\u044e\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f:*\n"]
                for p in pending[:10]:
                    did = p.get("deal_id", "?")
                    scheduled = common.fmt_ts(p.get("scheduled_at", 0))
                    delay = p.get("confirm_after_minutes", 0)
                    lines.append(f"- `{did[:8]}` | +{delay}\u043c\u0438\u043d | {scheduled}")
                text = "\n".join(lines)
            kb = tg_types.InlineKeyboardMarkup()
            for p in pending[:5]:
                did = p.get("deal_id", "")
                kb.row(tg_types.InlineKeyboardButton(
                    f"\u274c \u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c {did[:8]}",
                    callback_data=f"ac:cancel:{did}"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="ac:main"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def start_background(self, ctx: PluginContext) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_stop = False
        self._bg_thread = threading.Thread(
            target=self._check_loop,
            args=(ctx,),
            daemon=True,
            name="ac-check-loop",
        )
        self._bg_thread.start()

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        cfg = get_config()
        if not cfg.get("enabled", False):
            return False

        item_paid = getattr(EventTypes, "ITEM_PAID", None)
        item_sent = getattr(EventTypes, "ITEM_SENT", None)

        if (item_paid is not None and etype == item_paid) or \
           (item_sent is not None and etype == item_sent):
            deal_id = getattr(event, "deal_id", None) or getattr(event, "id", None)
            if deal_id:
                delay = cfg.get("delay_minutes", 15)
                add_pending(str(deal_id), delay)
                LOGGER.info("autoconfirm: scheduled deal %s in %d min", deal_id, delay)
            return False
        return False

    # --- Internal ---------------------------------------------------------

    def _check_loop(self, ctx: PluginContext) -> None:
        while not self._bg_stop:
            try:
                self._process_pending(ctx)
            except Exception:
                LOGGER.exception("autoconfirm: error in check loop")
            for _ in range(30):
                if self._bg_stop:
                    return
                time.sleep(1)

    def _process_pending(self, ctx: PluginContext) -> None:
        cfg = get_config()
        if not cfg.get("enabled", False):
            return
        if not ctx.playerok_acc:
            return

        with _pending_lock:
            pending = load_pending()
            now_ts = common.now()
            remaining = []
            for p in pending:
                scheduled_at = p.get("scheduled_at", 0)
                delay = p.get("confirm_after_minutes", 15)
                if now_ts - scheduled_at >= delay * 60:
                    deal_id = p.get("deal_id")
                    if deal_id:
                        try:
                            from playerokapi.enums import ItemDealStatuses
                            sent_status = getattr(ItemDealStatuses, "SENT", None)
                            if sent_status is not None:
                                ctx.playerok_acc.update_deal(deal_id, sent_status)
                            LOGGER.info("autoconfirm: sent deal %s", deal_id)
                            if cfg.get("notify_on_confirm", True):
                                try:
                                    ctx.bot.send_message(
                                        ctx.admin_id,
                                        f"\u23f2\ufe0f \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435: "
                                        f"\u0441\u0434\u0435\u043b\u043a\u0430 `{deal_id[:8]}` \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430.",
                                        parse_mode="Markdown"
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            LOGGER.exception("autoconfirm: failed to send %s", deal_id)
                            remaining.append(p)
                            continue
                else:
                    remaining.append(p)
            save_pending(remaining)


# --- Telegram helpers ------------------------------------------------------

def _send_or_edit(bot, chat_id: int, msg_id: int | None, text: str,
                  kb: Any = None, **kwargs) -> None:
    try:
        if msg_id:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, **kwargs)
        else:
            bot.send_message(chat_id, text, reply_markup=kb, **kwargs)
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb, **kwargs)
        except Exception:
            pass


HANDLER = Handler()
