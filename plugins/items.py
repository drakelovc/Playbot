"""Плагин items -- управление лотами из Telegram.

Позволяет:
  * Просматривать список активных лотов.
  * Редактировать цену и описание.
  * Публиковать/снимать лоты.

Telegram-команда -- /items.
Хранилище: storage/plugins/items/config.json.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.items")
STORAGE_DIR = os.path.join("storage", "plugins", "items")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "items_per_page": 10,
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
    id="items",
    name="\u041b\u043e\u0442\u044b",
    icon="\U0001f4e6",
    description=(
        "\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u043b\u043e\u0442\u0430\u043c\u0438: "
        "\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440, \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 "
        "\u0446\u0435\u043d\u044b \u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f. /items \u0434\u043b\u044f \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    ),
    instruction=(
        "*\U0001f4e6 \u041b\u043e\u0442\u044b*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u043b\u043e\u0442\u044b.\n"
        "- \u041f\u043e\u0437\u0432\u043e\u043b\u044f\u0435\u0442 \u043c\u0435\u043d\u044f\u0442\u044c "
        "\u0446\u0435\u043d\u0443 \u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435.\n"
        "- \u041f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u044f/\u0441\u043d\u044f\u0442\u0438\u0435 \u043b\u043e\u0442\u043e\u0432.\n\n"
        "*\u041a\u0430\u043a \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c:*\n"
        "1. `/items` - \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043b\u043e\u0442\u044b.\n"
        "2. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043b\u043e\u0442 \u0434\u043b\u044f \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0439."
    ),
    default_enabled=True,
    keywords=("\u043b\u043e\u0442", "item", "\u0442\u043e\u0432\u0430\u0440"),
)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Handler for the items plugin."""

    def setup(self, ctx: PluginContext) -> None:
        get_config()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["items"])
        def cmd_items(message):
            if message.from_user.id != admin_id:
                return
            _send_items_list(bot, ctx, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("it:"))
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

            if action == "list":
                _send_items_list(bot, ctx, chat_id, msg_id)
            elif action == "view" and len(parts) >= 3:
                item_id = parts[2]
                _send_item_detail(bot, ctx, chat_id, item_id, msg_id)
            elif action == "price" and len(parts) >= 3:
                item_id = parts[2]
                wait_state[chat_id] = {"step": "wait_price", "item_id": item_id}
                bot.send_message(chat_id, "\U0001f4b0 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 "
                                 "\u043d\u043e\u0432\u0443\u044e \u0446\u0435\u043d\u0443 (\u0432 \u0440\u0443\u0431\u043b\u044f\u0445):")
            elif action == "desc" and len(parts) >= 3:
                item_id = parts[2]
                wait_state[chat_id] = {"step": "wait_desc", "item_id": item_id}
                bot.send_message(chat_id, "\U0001f4dd \u0412\u0432\u0435\u0434\u0438\u0442\u0435 "
                                 "\u043d\u043e\u0432\u043e\u0435 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435:")
            elif action == "publish" and len(parts) >= 3:
                item_id = parts[2]
                _publish_item(bot, ctx, chat_id, item_id)
            elif action == "unpublish" and len(parts) >= 3:
                item_id = parts[2]
                _unpublish_item(bot, ctx, chat_id, item_id)

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
            content_types=["text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id, {})
            step = state.get("step")
            text = (message.text or "").strip()

            if step == "wait_price":
                item_id = state.get("item_id")
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "\u041d\u0443\u0436\u043d\u043e \u0446\u0435\u043b\u043e\u0435 "
                                     "\u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0435 \u0447\u0438\u0441\u043b\u043e.")
                    return
                wait_state.pop(message.chat.id, None)
                if ctx.playerok_acc and item_id:
                    try:
                        ctx.playerok_acc.update_item(item_id, price=val * 100)
                        bot.send_message(message.chat.id, f"\u2705 \u0426\u0435\u043d\u0430: {val} \u20bd")
                    except Exception as exc:
                        bot.send_message(message.chat.id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

            elif step == "wait_desc":
                item_id = state.get("item_id")
                wait_state.pop(message.chat.id, None)
                if ctx.playerok_acc and item_id and text:
                    try:
                        ctx.playerok_acc.update_item(item_id, description=text)
                        bot.send_message(message.chat.id, "\u2705 \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e.")
                    except Exception as exc:
                        bot.send_message(message.chat.id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

        def _send_items_list(b, context, tg_chat_id, edit_msg_id=None):
            if not context.playerok_acc:
                b.send_message(tg_chat_id, "\u274c \u0410\u043a\u043a\u0430\u0443\u043d\u0442 "
                               "\u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d.")
                return
            try:
                from playerokapi.enums import ItemStatuses
                approved = getattr(ItemStatuses, "APPROVED", None)
                statuses = [approved] if approved is not None else []
                item_list = context.playerok_acc.get_my_items(statuses=statuses, count=10)
                items = getattr(item_list, "items", []) or []
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")
                return

            if not items:
                text = "\U0001f4e6 \u041b\u043e\u0442\u043e\u0432 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
                kb = None
            else:
                text = "\U0001f4e6 *\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u043b\u043e\u0442\u044b:*\n"
                kb = tg_types.InlineKeyboardMarkup()
                for item in items[:10]:
                    iid = getattr(item, "id", "")
                    name = getattr(item, "name", "") or "?"
                    price = getattr(item, "price", 0) or 0
                    label = f"{name[:25]} | {price/100:.0f}\u20bd"
                    kb.row(tg_types.InlineKeyboardButton(
                        label, callback_data=f"it:view:{iid}"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_item_detail(b, context, tg_chat_id, item_id, edit_msg_id=None):
            text = f"\U0001f4e6 \u041b\u043e\u0442 `{item_id[:8]}...`\n"
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f4b0 \u0426\u0435\u043d\u0430", callback_data=f"it:price:{item_id}"),
                tg_types.InlineKeyboardButton(
                    "\U0001f4dd \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435", callback_data=f"it:desc:{item_id}"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f4e4 \u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c",
                    callback_data=f"it:publish:{item_id}"),
                tg_types.InlineKeyboardButton(
                    "\U0001f6d1 \u0421\u043d\u044f\u0442\u044c",
                    callback_data=f"it:unpublish:{item_id}"),
            )
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="it:list"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _publish_item(b, context, tg_chat_id, item_id):
            if not context.playerok_acc:
                return
            try:
                context.playerok_acc.publish_item(item_id)
                b.send_message(tg_chat_id, "\u2705 \u041b\u043e\u0442 \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d.")
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

        def _unpublish_item(b, context, tg_chat_id, item_id):
            if not context.playerok_acc:
                return
            try:
                from playerokapi.enums import ItemStatuses
                draft_status = getattr(ItemStatuses, "DRAFT", None)
                if draft_status is not None:
                    context.playerok_acc.update_item(item_id, status=draft_status)
                    b.send_message(tg_chat_id, "\u2705 \u041b\u043e\u0442 \u0441\u043d\u044f\u0442 \u0441 \u043f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u0438.")
                else:
                    b.send_message(tg_chat_id, "\u274c \u0421\u0442\u0430\u0442\u0443\u0441 DRAFT \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u0432 API.")
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
