"""Плагин deals -- управление сделками из Telegram.

Позволяет:
  * Просматривать активные и завершенные сделки.
  * Подтверждать сделки (отправить товар).
  * Оформлять возврат.

Telegram-команда -- /deals.
Хранилище: storage/plugins/deals/config.json.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.deals")
STORAGE_DIR = os.path.join("storage", "plugins", "deals")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "notify_new_deals": True,
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


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="deals",
    name="\u0421\u0434\u0435\u043b\u043a\u0438",
    icon="\U0001f91d",
    description=(
        "\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0441\u0434\u0435\u043b\u043a\u0430\u043c\u0438: "
        "\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440, \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 "
        "\u0438 \u0432\u043e\u0437\u0432\u0440\u0430\u0442. /deals \u0434\u043b\u044f \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    ),
    instruction=(
        "*\U0001f91d \u0421\u0434\u0435\u043b\u043a\u0438*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0435 "
        "\u0438 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043d\u044b\u0435 \u0441\u0434\u0435\u043b\u043a\u0438.\n"
        "- \u041f\u043e\u0437\u0432\u043e\u043b\u044f\u0435\u0442 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c "
        "\u0438\u043b\u0438 \u0432\u0435\u0440\u043d\u0443\u0442\u044c \u0441\u0434\u0435\u043b\u043a\u0443.\n\n"
        "*\u041a\u0430\u043a \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c:*\n"
        "1. `/deals` - \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a \u0441\u0434\u0435\u043b\u043e\u043a.\n"
        "2. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u0434\u0435\u043b\u043a\u0443 \u0434\u043b\u044f \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0439."
    ),
    default_enabled=True,
    keywords=("\u0441\u0434\u0435\u043b\u043a\u0430", "deal", "\u0432\u043e\u0437\u0432\u0440\u0430\u0442"),
)


# --- Status labels ---------------------------------------------------------

DEAL_STATUS_LABELS: dict[str, str] = {
    "PAID": "\U0001f4b0 \u041e\u043f\u043b\u0430\u0447\u0435\u043d\u0430",
    "PENDING": "\u23f3 \u041e\u0436\u0438\u0434\u0430\u043d\u0438\u0435",
    "SENT": "\U0001f4e6 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430",
    "CONFIRMED": "\u2705 \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
    "CONFIRMED_AUTOMATICALLY": "\u2705 \u0410\u0432\u0442\u043e",
    "ROLLED_BACK": "\u21a9\ufe0f \u0412\u043e\u0437\u0432\u0440\u0430\u0442",
}


def _status_label(status) -> str:
    name = getattr(status, "name", str(status))
    return DEAL_STATUS_LABELS.get(name, str(name))


# --- Handler ---------------------------------------------------------------

class Handler:
    """Handler for the deals plugin."""

    def setup(self, ctx: PluginContext) -> None:
        get_config()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id

        @bot.message_handler(commands=["deals"])
        def cmd_deals(message):
            if message.from_user.id != admin_id:
                return
            _send_deals_panel(bot, ctx, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dl:"))
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

            if action == "active":
                _send_deals_list(bot, ctx, chat_id, "active", msg_id)
            elif action == "completed":
                _send_deals_list(bot, ctx, chat_id, "completed", msg_id)
            elif action == "confirm" and len(parts) >= 3:
                deal_id = parts[2]
                _confirm_deal(bot, ctx, chat_id, deal_id)
            elif action == "refund" and len(parts) >= 3:
                deal_id = parts[2]
                _refund_deal(bot, ctx, chat_id, deal_id)
            elif action == "main":
                _send_deals_panel(bot, ctx, chat_id, msg_id)

        def _send_deals_panel(b, context, tg_chat_id, edit_msg_id=None):
            text = (
                "\U0001f91d *\u0421\u0434\u0435\u043b\u043a\u0438*\n\n"
                "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e:"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f525 \u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435",
                    callback_data="dl:active"),
                tg_types.InlineKeyboardButton(
                    "\u2705 \u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043d\u044b\u0435",
                    callback_data="dl:completed"),
            )
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_deals_list(b, context, tg_chat_id, category, edit_msg_id=None):
            if not context.playerok_acc:
                b.send_message(tg_chat_id, "\u274c \u0410\u043a\u043a\u0430\u0443\u043d\u0442 "
                               "\u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d.")
                return
            try:
                from playerokapi.enums import ItemDealStatuses
                if category == "active":
                    statuses = []
                    for s in ("PAID", "PENDING", "SENT"):
                        val = getattr(ItemDealStatuses, s, None)
                        if val is not None:
                            statuses.append(val)
                    deal_list = context.playerok_acc.get_deals(statuses=statuses, count=10)
                else:
                    statuses = []
                    for s in ("CONFIRMED", "CONFIRMED_AUTOMATICALLY"):
                        val = getattr(ItemDealStatuses, s, None)
                        if val is not None:
                            statuses.append(val)
                    deal_list = context.playerok_acc.get_deals(statuses=statuses, count=10)
                deals = getattr(deal_list, "items", []) or []
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")
                return

            if not deals:
                text = "\U0001f91d \u0421\u0434\u0435\u043b\u043e\u043a \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
                kb = tg_types.InlineKeyboardMarkup()
            else:
                lines = ["\U0001f91d *\u0421\u0434\u0435\u043b\u043a\u0438:*\n"]
                kb = tg_types.InlineKeyboardMarkup()
                for d in deals[:10]:
                    did = getattr(d, "id", "")
                    item_name = getattr(d, "item_name", "") or getattr(d, "name", "") or "?"
                    status = getattr(d, "status", None)
                    price = getattr(d, "price", 0) or 0
                    lines.append(
                        f"- {common.md_escape(item_name[:30])} | "
                        f"{price/100:.0f}\u20bd | {_status_label(status)}"
                    )
                    if category == "active":
                        kb.row(
                            tg_types.InlineKeyboardButton(
                                f"\u2705 {item_name[:15]}", callback_data=f"dl:confirm:{did}"),
                            tg_types.InlineKeyboardButton(
                                f"\u21a9\ufe0f {item_name[:15]}", callback_data=f"dl:refund:{did}"),
                        )
                text = "\n".join(lines)

            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="dl:main"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _confirm_deal(b, context, tg_chat_id, deal_id):
            if not context.playerok_acc:
                return
            try:
                from playerokapi.enums import ItemDealStatuses
                sent_status = getattr(ItemDealStatuses, "SENT", None)
                if sent_status is not None:
                    context.playerok_acc.update_deal(deal_id, sent_status)
                b.send_message(tg_chat_id, "\u2705 \u0421\u0434\u0435\u043b\u043a\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430.")
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

        def _refund_deal(b, context, tg_chat_id, deal_id):
            if not context.playerok_acc:
                return
            try:
                from playerokapi.enums import ItemDealStatuses
                rolled_back = getattr(ItemDealStatuses, "ROLLED_BACK", None)
                if rolled_back is not None:
                    context.playerok_acc.update_deal(deal_id, rolled_back)
                b.send_message(tg_chat_id, "\u21a9\ufe0f \u0412\u043e\u0437\u0432\u0440\u0430\u0442 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d.")
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        return False


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
