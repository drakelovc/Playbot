"""Plugin custom_commands -- custom keyword-response commands for buyers in Playerok chat.

Admins define keyword triggers via Telegram. When a buyer sends a message
matching a keyword (exact or contains), the bot auto-replies with the
configured response via playerok_acc.send_message().

Telegram command: /customcmd
Storage: storage/plugins/custom_commands/commands.json
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.custom_commands")
STORAGE_DIR = os.path.join("storage", "plugins", "custom_commands")
CONFIG_FILE = os.path.join(STORAGE_DIR, "commands.json")

DEFAULT_CONFIG: list[dict[str, Any]] = []

# Per-chat cooldown: minimum seconds between responses to the same chat
_COOLDOWN_SECONDS = 10
_last_reply: dict[str, float] = {}  # chat_id -> last reply timestamp


def get_commands() -> list[dict[str, Any]]:
    """Load commands list from storage."""
    common.ensure_dir(STORAGE_DIR)
    cmds = common.load_json(CONFIG_FILE, None)
    if cmds is None:
        cmds = list(DEFAULT_CONFIG)
        common.save_json(CONFIG_FILE, cmds)
    return cmds


def save_commands(cmds: list[dict[str, Any]]) -> None:
    common.ensure_dir(STORAGE_DIR)
    common.save_json(CONFIG_FILE, cmds)


def match_command(text: str, commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find first matching enabled command for given message text."""
    if not text:
        return None
    msg_lower = text.lower().strip()
    for cmd in commands:
        if not cmd.get("enabled", True):
            continue
        keyword = cmd.get("keyword", "").lower().strip()
        if not keyword:
            continue
        match_type = cmd.get("match_type", "exact")
        if match_type == "exact":
            if msg_lower == keyword:
                return cmd
        elif match_type == "contains":
            if keyword in msg_lower:
                return cmd
    return None


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="custom_commands",
    name="\u041a\u0430\u0441\u0442\u043e\u043c\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b",
    icon="\U0001f916",
    description=(
        "\u041a\u0430\u0441\u0442\u043e\u043c\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b-\u043e\u0442\u0432\u0435\u0442\u044b \u0434\u043b\u044f \u043f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0435\u0439 \u0432 \u0447\u0430\u0442\u0435 Playerok. "
        "/customcmd \u0434\u043b\u044f \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    ),
    instruction=(
        "*\U0001f916 \u041a\u0430\u0441\u0442\u043e\u043c\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u0410\u0432\u0442\u043e\u043e\u0442\u0432\u0435\u0442 \u043d\u0430 \u043a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0441\u043b\u043e\u0432\u0430 \u043f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0435\u0439 \u0432 \u0447\u0430\u0442\u0435 Playerok.\n"
        "- \u041f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0430 \u0442\u043e\u0447\u043d\u043e\u0433\u043e \u0438 \u0447\u0430\u0441\u0442\u0438\u0447\u043d\u043e\u0433\u043e \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u044f.\n\n"
        "*\u041a\u0430\u043a \u043d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c:*\n"
        "1. `/customcmd` - \u043e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f.\n"
        "2. \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043a\u043e\u043c\u0430\u043d\u0434\u0443 (\u043a\u043b\u044e\u0447\u0435\u0432\u043e\u0435 \u0441\u043b\u043e\u0432\u043e + \u043e\u0442\u0432\u0435\u0442).\n"
        "3. \u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c/\u0432\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043a\u043e\u043c\u0430\u043d\u0434\u044b \u043f\u043e \u043e\u0442\u0434\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u0438."
    ),
    default_enabled=True,
    keywords=("\u043a\u043e\u043c\u0430\u043d\u0434\u044b", "custom", "\u0430\u0432\u0442\u043e\u043e\u0442\u0432\u0435\u0442"),
)


class Handler:
    """Main handler for the custom_commands plugin."""

    def setup(self, ctx: PluginContext) -> None:
        get_commands()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["customcmd"])
        def cmd_customcmd(message):
            if message.from_user.id != admin_id:
                return
            _send_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ccmd:"))
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
                _send_panel(bot, chat_id, msg_id)
            elif action == "add":
                wait_state[chat_id] = {"step": "wait_keyword"}
                bot.send_message(
                    chat_id,
                    "\U0001f916 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043a\u043b\u044e\u0447\u0435\u0432\u043e\u0435 \u0441\u043b\u043e\u0432\u043e (\u0442\u0440\u0438\u0433\u0433\u0435\u0440):\n\n\u041e\u0442\u043c\u0435\u043d\u0430: /cancel"
                )
            elif action == "del":
                cmds = get_commands()
                if not cmds:
                    bot.send_message(chat_id, "\u041d\u0435\u0442 \u043a\u043e\u043c\u0430\u043d\u0434.")
                    return
                kb = tg_types.InlineKeyboardMarkup()
                for i, cmd in enumerate(cmds):
                    kb.row(tg_types.InlineKeyboardButton(
                        f"\u274c {cmd['keyword']}", callback_data=f"ccmd:rm:{i}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="ccmd:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f5d1 \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0434\u043b\u044f \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u044f:", kb)
            elif action == "rm" and len(parts) >= 3:
                try:
                    idx = int(parts[2])
                    cmds = get_commands()
                    if 0 <= idx < len(cmds):
                        removed = cmds.pop(idx)
                        save_commands(cmds)
                        bot.send_message(chat_id,
                                         f"\u2705 \u0423\u0434\u0430\u043b\u0435\u043d\u0430: {removed['keyword']}")
                except (ValueError, IndexError):
                    pass
                _send_panel(bot, chat_id)
            elif action == "toggle" and len(parts) >= 3:
                try:
                    idx = int(parts[2])
                    cmds = get_commands()
                    if 0 <= idx < len(cmds):
                        cmds[idx]["enabled"] = not cmds[idx].get("enabled", True)
                        save_commands(cmds)
                except (ValueError, IndexError):
                    pass
                _send_panel(bot, chat_id, msg_id)
            elif action == "type" and len(parts) >= 3:
                # Toggle match_type between exact and contains
                try:
                    idx = int(parts[2])
                    cmds = get_commands()
                    if 0 <= idx < len(cmds):
                        current = cmds[idx].get("match_type", "exact")
                        cmds[idx]["match_type"] = "contains" if current == "exact" else "exact"
                        save_commands(cmds)
                except (ValueError, IndexError):
                    pass
                _send_panel(bot, chat_id, msg_id)

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
            content_types=["text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id)
            if not state:
                return
            step = state.get("step")
            text = (message.text or "").strip()

            if text.startswith("/"):
                wait_state.pop(message.chat.id, None)
                return

            if step == "wait_keyword":
                if not text:
                    bot.send_message(message.chat.id, "\u041d\u0443\u0436\u043d\u043e \u0432\u0432\u0435\u0441\u0442\u0438 \u043a\u043b\u044e\u0447\u0435\u0432\u043e\u0435 \u0441\u043b\u043e\u0432\u043e.")
                    return
                wait_state[message.chat.id] = {"step": "wait_response", "keyword": text}
                bot.send_message(
                    message.chat.id,
                    f"\u041a\u043b\u044e\u0447\u0435\u0432\u043e\u0435 \u0441\u043b\u043e\u0432\u043e: *{text}*\n\n"
                    "\u0422\u0435\u043f\u0435\u0440\u044c \u0432\u0432\u0435\u0434\u0438\u0442\u0435 \u0442\u0435\u043a\u0441\u0442 \u043e\u0442\u0432\u0435\u0442\u0430:\n\n\u041e\u0442\u043c\u0435\u043d\u0430: /cancel",
                    parse_mode="Markdown",
                )
            elif step == "wait_response":
                if not text:
                    bot.send_message(message.chat.id, "\u041d\u0443\u0436\u043d\u043e \u0432\u0432\u0435\u0441\u0442\u0438 \u043e\u0442\u0432\u0435\u0442.")
                    return
                keyword = state.get("keyword", "")
                cmds = get_commands()
                cmds.append({
                    "keyword": keyword,
                    "response": text,
                    "enabled": True,
                    "match_type": "exact",
                })
                save_commands(cmds)
                wait_state.pop(message.chat.id, None)
                bot.send_message(
                    message.chat.id,
                    f"\u2705 \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0430:\n"
                    f"\u041a\u043b\u044e\u0447: *{keyword}*\n\u041e\u0442\u0432\u0435\u0442: {text}",
                    parse_mode="Markdown",
                )
                _send_panel(bot, message.chat.id)

        def _send_panel(b, chat_id: int, edit_msg_id: int | None = None):
            cmds = get_commands()
            if not cmds:
                text = "\U0001f916 *\u041a\u0430\u0441\u0442\u043e\u043c\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b*\n\n\u041f\u043e\u043a\u0430 \u043d\u0435\u0442 \u043a\u043e\u043c\u0430\u043d\u0434."
            else:
                lines = ["\U0001f916 *\u041a\u0430\u0441\u0442\u043e\u043c\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b*\n"]
                for i, cmd in enumerate(cmds):
                    icon = "\u2705" if cmd.get("enabled", True) else "\u274c"
                    mt = "\U0001f3af" if cmd.get("match_type", "exact") == "exact" else "\U0001f50d"
                    lines.append(f"{icon} {mt} `{cmd['keyword']}` \u2192 {cmd['response'][:40]}")
                text = "\n".join(lines)

            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c", callback_data="ccmd:add"))
            if cmds:
                kb.row(tg_types.InlineKeyboardButton(
                    "\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data="ccmd:del"))
                for i, cmd in enumerate(cmds):
                    icon = "\u2705" if cmd.get("enabled", True) else "\u274c"
                    mt_label = "\U0001f3af\u0422\u043e\u0447\u043d\u043e" if cmd.get("match_type", "exact") == "exact" else "\U0001f50d\u0421\u043e\u0434\u0435\u0440\u0436\u0438\u0442"
                    kb.row(
                        tg_types.InlineKeyboardButton(
                            f"{icon} {cmd['keyword'][:15]}", callback_data=f"ccmd:toggle:{i}"),
                        tg_types.InlineKeyboardButton(
                            mt_label, callback_data=f"ccmd:type:{i}"),
                    )
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes
        try:
            etype = event.type
        except Exception:
            return False

        new_message_type = getattr(EventTypes, "NEW_MESSAGE", None)
        if new_message_type is None or etype != new_message_type:
            return False

        msg_text = getattr(event, "text", None) or getattr(event, "message", None) or ""
        if not msg_text:
            return False

        chat_id = getattr(event, "chat_id", None) or getattr(event, "chatId", None)
        if not chat_id:
            return False

        # Per-chat cooldown to prevent spam
        now = time.time()
        last = _last_reply.get(str(chat_id), 0)
        if now - last < _COOLDOWN_SECONDS:
            return False

        cmds = get_commands()
        matched = match_command(str(msg_text), cmds)
        if matched is None:
            return False

        try:
            if ctx.playerok_acc:
                ctx.playerok_acc.send_message(chat_id, matched["response"])
                _last_reply[str(chat_id)] = now
                LOGGER.info("custom_commands: replied to '%s' with command '%s'",
                            msg_text[:50], matched["keyword"])
        except Exception as exc:
            LOGGER.error("custom_commands: send_message failed: %s", exc)

        return False  # don't claim exclusive handling


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
