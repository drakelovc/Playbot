"""Плагин reviews -- просмотр отзывов и уведомления о новых.

Позволяет:
  * Просматривать список отзывов с фильтрацией по рейтингу.
  * Получать уведомления о новых отзывах.

Telegram-команда -- /reviews.
Хранилище: storage/plugins/reviews/config.json.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.reviews")
STORAGE_DIR = os.path.join("storage", "plugins", "reviews")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "notify_new_reviews": True,
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
    id="reviews",
    name="\u041e\u0442\u0437\u044b\u0432\u044b",
    icon="\u2b50",
    description=(
        "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u043e\u0442\u0437\u044b\u0432\u043e\u0432 "
        "\u0438 \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f \u043e \u043d\u043e\u0432\u044b\u0445. "
        "/reviews \u0434\u043b\u044f \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0430."
    ),
    instruction=(
        "*\u2b50 \u041e\u0442\u0437\u044b\u0432\u044b*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0441\u043f\u0438\u0441\u043e\u043a "
        "\u043e\u0442\u0437\u044b\u0432\u043e\u0432 \u0441 \u0444\u0438\u043b\u044c\u0442\u0440\u0430\u0446\u0438\u0435\u0439.\n"
        "- \u0423\u0432\u0435\u0434\u043e\u043c\u043b\u044f\u0435\u0442 \u043e "
        "\u043d\u043e\u0432\u044b\u0445 \u043e\u0442\u0437\u044b\u0432\u0430\u0445.\n\n"
        "*\u041a\u0430\u043a \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c:*\n"
        "1. `/reviews` - \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043e\u0442\u0437\u044b\u0432\u044b.\n"
        "2. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 \u0444\u0438\u043b\u044c\u0442\u0440\u044b "
        "\u043f\u043e \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443."
    ),
    default_enabled=True,
    keywords=("\u043e\u0442\u0437\u044b\u0432", "review", "\u0440\u0435\u0439\u0442\u0438\u043d\u0433"),
)


# --- Helper ----------------------------------------------------------------

def _stars(rating: int) -> str:
    return "\u2b50" * max(0, min(5, rating))


# --- Handler ---------------------------------------------------------------

class Handler:
    """Handler for the reviews plugin."""

    def setup(self, ctx: PluginContext) -> None:
        get_config()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id

        @bot.message_handler(commands=["reviews"])
        def cmd_reviews(message):
            if message.from_user.id != admin_id:
                return
            _send_reviews_panel(bot, ctx, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rv:"))
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

            if action == "all":
                _send_reviews_list(bot, ctx, chat_id, rating_filter=None, edit_msg_id=msg_id)
            elif action == "pos":
                _send_reviews_list(bot, ctx, chat_id, rating_filter="positive", edit_msg_id=msg_id)
            elif action == "neg":
                _send_reviews_list(bot, ctx, chat_id, rating_filter="negative", edit_msg_id=msg_id)
            elif action == "toggle":
                cfg = get_config()
                cfg["notify_new_reviews"] = not cfg.get("notify_new_reviews", True)
                save_config(cfg)
                _send_reviews_panel(bot, ctx, chat_id, msg_id)

        def _send_reviews_panel(b, context, tg_chat_id, edit_msg_id=None):
            cfg = get_config()
            notify = cfg.get("notify_new_reviews", True)
            notify_icon = "\u2705" if notify else "\u274c"
            text = (
                "\u2b50 *\u041e\u0442\u0437\u044b\u0432\u044b*\n\n"
                f"\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f: {notify_icon}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f4cb \u0412\u0441\u0435", callback_data="rv:all"),
                tg_types.InlineKeyboardButton(
                    "\u2705 \u041f\u043e\u043b\u043e\u0436.", callback_data="rv:pos"),
                tg_types.InlineKeyboardButton(
                    "\u274c \u041e\u0442\u0440\u0438\u0446.", callback_data="rv:neg"),
            )
            toggle_label = "\U0001f515 \u0412\u044b\u043a\u043b." if notify else "\U0001f514 \u0412\u043a\u043b."
            kb.row(tg_types.InlineKeyboardButton(toggle_label, callback_data="rv:toggle"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_reviews_list(b, context, tg_chat_id, rating_filter=None, edit_msg_id=None):
            if not context.playerok_acc:
                b.send_message(tg_chat_id, "\u274c \u0410\u043a\u043a\u0430\u0443\u043d\u0442 "
                               "\u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d.")
                return
            try:
                review_list = context.playerok_acc.get_my_reviews(count=10)
                reviews = getattr(review_list, "reviews", []) or []
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")
                return

            # Filter
            if rating_filter == "positive":
                reviews = [r for r in reviews if (getattr(r, "rating", 0) or 0) >= 4]
            elif rating_filter == "negative":
                reviews = [r for r in reviews if (getattr(r, "rating", 0) or 0) <= 2]

            if not reviews:
                text = "\u2b50 \u041e\u0442\u0437\u044b\u0432\u043e\u0432 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e."
            else:
                lines = ["\u2b50 *\u041e\u0442\u0437\u044b\u0432\u044b:*\n"]
                for r in reviews[:10]:
                    rating = getattr(r, "rating", 0) or 0
                    text_body = getattr(r, "text", "") or ""
                    buyer = getattr(r, "buyer_name", "") or getattr(r, "user_name", "") or "?"
                    lines.append(f"{_stars(rating)} | {common.md_escape(buyer)}")
                    if text_body:
                        lines.append(f"  {common.md_escape(text_body[:100])}")
                text = "\n".join(lines)

            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="rv:all"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        new_review = getattr(EventTypes, "NEW_REVIEW", None)
        if new_review is not None and etype == new_review:
            cfg = get_config()
            if cfg.get("notify_new_reviews", True):
                rating = getattr(event, "rating", 0) or 0
                text_body = getattr(event, "text", "") or ""
                buyer = getattr(event, "buyer_name", "") or getattr(event, "user_name", "") or "?"
                msg = (
                    f"\u2b50 *\u041d\u043e\u0432\u044b\u0439 \u043e\u0442\u0437\u044b\u0432*\n"
                    f"\u041e\u0442: {common.md_escape(buyer)}\n"
                    f"\u0420\u0435\u0439\u0442\u0438\u043d\u0433: {_stars(rating)}\n"
                    f"\u0422\u0435\u043a\u0441\u0442: {common.md_escape(text_body[:200])}"
                )
                try:
                    ctx.bot.send_message(ctx.admin_id, msg, parse_mode="Markdown")
                except Exception:
                    try:
                        ctx.bot.send_message(ctx.admin_id, msg)
                    except Exception:
                        pass
            return False
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
