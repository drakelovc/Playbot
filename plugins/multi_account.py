"""Plugin multi_account -- manage multiple Playerok shops from one bot.

Features:
- Store multiple Playerok accounts with alias, cookies, proxy, status
- Each account gets its own EventListener in a background thread
- Non-active account events send notifications prefixed with [alias]
- Switch active account to change ctx.playerok_acc for other plugins
- Add/Remove/Refresh accounts via Telegram inline UI

Telegram command: /accounts
Storage: storage/plugins/multi_account/accounts.json
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

LOGGER = logging.getLogger("playerok_bot.multi_account")
STORAGE_DIR = os.path.join("storage", "plugins", "multi_account")
ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")

# --- Storage helpers -------------------------------------------------------


def load_accounts() -> list[dict[str, Any]]:
    common.ensure_dir(STORAGE_DIR)
    data = common.load_json(ACCOUNTS_FILE, None)
    if data is None:
        data = []
        common.save_json(ACCOUNTS_FILE, data)
    return data


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    common.ensure_dir(STORAGE_DIR)
    common.save_json(ACCOUNTS_FILE, accounts)


def find_account(alias: str) -> dict[str, Any] | None:
    for acc in load_accounts():
        if acc.get("alias") == alias:
            return acc
    return None


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="multi_account",
    name="Multi-Account",
    icon="\U0001f465",
    description=(
        "Manage multiple Playerok shops from one bot. "
        "/accounts for management."
    ),
    instruction=(
        "*\U0001f465 Multi-Account*\n\n"
        "*What this plugin does:*\n"
        "- Manage multiple Playerok accounts from one bot instance.\n"
        "- Each account gets its own event listener for real-time notifications.\n"
        "- Switch the active account to control which shop other plugins use.\n\n"
        "*How to set up:*\n"
        "1. Enable the plugin.\n"
        "2. `/accounts` - add accounts, switch active, monitor status.\n"
        "3. Non-active accounts send notifications prefixed with their alias."
    ),
    default_enabled=True,
    keywords=("accounts", "multi", "shop"),
)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Main handler for multi_account plugin."""

    def __init__(self):
        self._instances: dict[str, dict[str, Any]] = {}
        # {alias: {"account": Account, "thread": Thread|None, "stop": bool}}
        self._lock = threading.Lock()
        self._ctx: PluginContext | None = None

    def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        load_accounts()  # ensure storage file exists

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["accounts"])
        def cmd_accounts(message):
            if message.from_user.id != admin_id:
                return
            _send_main_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ma:"))
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

            elif action == "add":
                wait_state[chat_id] = {"step": "wait_alias"}
                bot.send_message(
                    chat_id,
                    "\U0001f465 Enter alias for the new account:\n\nCancel: /cancel"
                )

            elif action == "remove":
                accounts = load_accounts()
                if not accounts:
                    bot.send_message(chat_id, "No accounts configured.")
                    return
                kb = tg_types.InlineKeyboardMarkup()
                for acc in accounts:
                    kb.row(tg_types.InlineKeyboardButton(
                        f"\u274c {acc['alias']}", callback_data=f"ma:rm:{acc['alias']}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 Back", callback_data="ma:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f5d1 Select account to remove:", kb)

            elif action == "rm" and len(parts) >= 3:
                alias = ":".join(parts[2:])
                self._remove_account(alias)
                bot.send_message(chat_id, f"\u2705 Account '{alias}' removed.")
                _send_main_panel(bot, chat_id)

            elif action == "switch":
                accounts = load_accounts()
                if not accounts:
                    bot.send_message(chat_id, "No accounts configured.")
                    return
                kb = tg_types.InlineKeyboardMarkup()
                for acc in accounts:
                    marker = "\u2705 " if acc.get("is_active") else ""
                    kb.row(tg_types.InlineKeyboardButton(
                        f"{marker}{acc['alias']}", callback_data=f"ma:sw:{acc['alias']}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 Back", callback_data="ma:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f504 Select account to activate:", kb)

            elif action == "sw" and len(parts) >= 3:
                alias = ":".join(parts[2:])
                ok = self._switch_active(alias, ctx)
                if ok:
                    bot.send_message(chat_id, f"\u2705 Active account: {alias}")
                else:
                    bot.send_message(chat_id, f"\u274c Account '{alias}' not found.")
                _send_main_panel(bot, chat_id)

            elif action == "refresh":
                accounts = load_accounts()
                if not accounts:
                    bot.send_message(chat_id, "No accounts configured.")
                    return
                kb = tg_types.InlineKeyboardMarkup()
                for acc in accounts:
                    kb.row(tg_types.InlineKeyboardButton(
                        f"\U0001f504 {acc['alias']}", callback_data=f"ma:ref:{acc['alias']}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 Back", callback_data="ma:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f504 Select account to refresh:", kb)

            elif action == "ref" and len(parts) >= 3:
                alias = ":".join(parts[2:])
                bot.send_message(chat_id, f"\u23f3 Refreshing '{alias}'...")
                threading.Thread(
                    target=self._refresh_account,
                    args=(alias, ctx),
                    daemon=True,
                ).start()

        @bot.message_handler(commands=["cancel"])
        def cancel_ma(message):
            if message.from_user.id != admin_id:
                return
            if message.chat.id in wait_state:
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, "Cancelled.")

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
            content_types=["text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id)
            if state is None:
                return
            step = state.get("step")
            text = (message.text or "").strip()

            if step == "wait_alias":
                if not text:
                    bot.send_message(message.chat.id, "Alias cannot be empty.")
                    return
                if find_account(text):
                    bot.send_message(message.chat.id, "Alias already exists. Choose another.")
                    return
                state["alias"] = text
                state["step"] = "wait_cookies"
                bot.send_message(
                    message.chat.id,
                    "Enter cookies for this account:\n\nCancel: /cancel"
                )

            elif step == "wait_cookies":
                if not text:
                    bot.send_message(message.chat.id, "Cookies cannot be empty.")
                    return
                state["cookies"] = text
                state["step"] = "wait_user_agent"
                bot.send_message(
                    message.chat.id,
                    "Enter user_agent (or send '-' to skip):\n\nCancel: /cancel"
                )

            elif step == "wait_user_agent":
                ua = "" if text == "-" else text
                state["user_agent"] = ua
                state["step"] = "wait_proxy"
                bot.send_message(
                    message.chat.id,
                    "Enter proxy (or send '-' to skip):\n\nCancel: /cancel"
                )

            elif step == "wait_proxy":
                proxy = "" if text == "-" else text
                state["proxy"] = proxy
                # Validate account
                alias = state["alias"]
                cookies = state["cookies"]
                user_agent = state.get("user_agent", "")
                wait_state.pop(message.chat.id, None)

                bot.send_message(message.chat.id, "\u23f3 Validating account...")
                threading.Thread(
                    target=self._add_account_async,
                    args=(alias, cookies, user_agent, proxy, ctx),
                    daemon=True,
                ).start()

        def _send_main_panel(b, chat_id: int, edit_msg_id: int | None = None):
            accounts = load_accounts()
            if not accounts:
                text = "\U0001f465 *Multi-Account*\n\nNo accounts configured."
            else:
                lines = ["\U0001f465 *Multi-Account*\n"]
                for acc in accounts:
                    status = acc.get("status", "offline")
                    if status == "online":
                        icon = "\U0001f7e2"
                    elif status == "error":
                        icon = "\U0001f534"
                    else:
                        icon = "\u26aa"
                    active_mark = " \u2b50" if acc.get("is_active") else ""
                    username = acc.get("username") or "unknown"
                    lines.append(f"{icon} *{acc['alias']}*{active_mark} (@{username})")
                    if acc.get("error"):
                        lines.append(f"    \u26a0 {acc['error'][:60]}")
                text = "\n".join(lines)

            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u2795 Add", callback_data="ma:add"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u274c Remove", callback_data="ma:remove"))
            kb.row(tg_types.InlineKeyboardButton(
                "\U0001f504 Switch active", callback_data="ma:switch"))
            kb.row(tg_types.InlineKeyboardButton(
                "\U0001f504 Refresh", callback_data="ma:refresh"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def start_background(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        accounts = load_accounts()
        for acc in accounts:
            alias = acc.get("alias", "")
            if not acc.get("cookies"):
                continue
            self._start_account_listener(alias, acc, ctx)

    # --- Internal methods --------------------------------------------------

    def _add_account_async(self, alias: str, cookies: str, user_agent: str,
                           proxy: str, ctx: PluginContext) -> None:
        """Validate and add account (runs in a thread)."""
        try:
            from playerokapi.account import Account as PlayerokAccount

            kwargs: dict[str, Any] = {"cookies": cookies}
            if user_agent:
                kwargs["user_agent"] = user_agent
            if proxy:
                kwargs["proxy"] = proxy

            acc_instance = PlayerokAccount(**kwargs)
            acc_instance.get()
            username = getattr(acc_instance, "username", None) or ""

            account_data: dict[str, Any] = {
                "alias": alias,
                "cookies": cookies,
                "user_agent": user_agent,
                "proxy": proxy,
                "is_active": False,
                "status": "online",
                "username": username,
                "error": None,
            }

            accounts = load_accounts()
            # If no accounts yet, make this one active
            if not accounts:
                account_data["is_active"] = True
                ctx.playerok_acc = acc_instance
            accounts.append(account_data)
            save_accounts(accounts)

            with self._lock:
                self._instances[alias] = {
                    "account": acc_instance,
                    "thread": None,
                    "stop": False,
                }

            # Start listener for non-active accounts
            if not account_data["is_active"]:
                self._start_account_listener(alias, account_data, ctx)

            _notify_admin(ctx, f"\u2705 Account '{alias}' added (@{username}).")

        except Exception as exc:
            error_msg = str(exc)[:200]
            _notify_admin(ctx, f"\u274c Failed to add '{alias}': {error_msg}")

    def _remove_account(self, alias: str) -> None:
        """Stop listener and remove account from storage."""
        self._stop_listener(alias)

        with self._lock:
            self._instances.pop(alias, None)

        accounts = load_accounts()
        accounts = [a for a in accounts if a.get("alias") != alias]
        save_accounts(accounts)

    def _switch_active(self, alias: str, ctx: PluginContext) -> bool:
        """Switch the active account. Returns True on success."""
        accounts = load_accounts()
        found = False
        for acc in accounts:
            if acc["alias"] == alias:
                acc["is_active"] = True
                found = True
            else:
                acc["is_active"] = False
        if not found:
            return False
        save_accounts(accounts)

        # Update ctx.playerok_acc
        with self._lock:
            info = self._instances.get(alias)
            if info and info.get("account"):
                ctx.playerok_acc = info["account"]

        # Restart listeners: stop listener for the newly active account,
        # start listeners for newly non-active accounts
        for acc in accounts:
            a = acc["alias"]
            if acc["is_active"]:
                self._stop_listener(a)
            else:
                # Ensure listener is running
                with self._lock:
                    info = self._instances.get(a)
                if info and (info.get("thread") is None or not info["thread"].is_alive()):
                    self._start_account_listener(a, acc, ctx)

        return True

    def _refresh_account(self, alias: str, ctx: PluginContext) -> None:
        """Re-initialize a specific account (runs in a thread)."""
        accounts = load_accounts()
        acc_data = None
        for a in accounts:
            if a["alias"] == alias:
                acc_data = a
                break
        if acc_data is None:
            return

        self._stop_listener(alias)

        try:
            from playerokapi.account import Account as PlayerokAccount

            kwargs: dict[str, Any] = {"cookies": acc_data["cookies"]}
            if acc_data.get("user_agent"):
                kwargs["user_agent"] = acc_data["user_agent"]
            if acc_data.get("proxy"):
                kwargs["proxy"] = acc_data["proxy"]

            acc_instance = PlayerokAccount(**kwargs)
            acc_instance.get()
            username = getattr(acc_instance, "username", None) or ""

            acc_data["status"] = "online"
            acc_data["username"] = username
            acc_data["error"] = None
            save_accounts(accounts)

            with self._lock:
                self._instances[alias] = {
                    "account": acc_instance,
                    "thread": None,
                    "stop": False,
                }

            if acc_data.get("is_active"):
                ctx.playerok_acc = acc_instance
            else:
                self._start_account_listener(alias, acc_data, ctx)

            _notify_admin(ctx, f"\u2705 Account '{alias}' refreshed (@{username}).")

        except Exception as exc:
            error_msg = str(exc)[:200]
            acc_data["status"] = "error"
            acc_data["error"] = error_msg
            save_accounts(accounts)
            _notify_admin(ctx, f"\u274c Refresh failed for '{alias}': {error_msg}")

    def _start_account_listener(self, alias: str, acc_data: dict[str, Any],
                                ctx: PluginContext) -> None:
        """Start an EventListener thread for a non-active account."""
        with self._lock:
            info = self._instances.get(alias)
            if info is None:
                # Initialize the account instance
                try:
                    from playerokapi.account import Account as PlayerokAccount

                    kwargs: dict[str, Any] = {"cookies": acc_data["cookies"]}
                    if acc_data.get("user_agent"):
                        kwargs["user_agent"] = acc_data["user_agent"]
                    if acc_data.get("proxy"):
                        kwargs["proxy"] = acc_data["proxy"]

                    acc_instance = PlayerokAccount(**kwargs)
                    acc_instance.get()
                    username = getattr(acc_instance, "username", None) or ""

                    # Update stored data
                    accounts = load_accounts()
                    for a in accounts:
                        if a["alias"] == alias:
                            a["status"] = "online"
                            a["username"] = username
                            a["error"] = None
                            break
                    save_accounts(accounts)

                    info = {"account": acc_instance, "thread": None, "stop": False}
                    self._instances[alias] = info

                except Exception as exc:
                    error_msg = str(exc)[:200]
                    accounts = load_accounts()
                    for a in accounts:
                        if a["alias"] == alias:
                            a["status"] = "error"
                            a["error"] = error_msg
                            break
                    save_accounts(accounts)
                    LOGGER.error("multi_account: failed to init %s: %s", alias, exc)
                    return

            if info.get("thread") and info["thread"].is_alive():
                return  # already running

            info["stop"] = False
            t = threading.Thread(
                target=self._listener_loop,
                args=(alias, info, ctx),
                daemon=True,
                name=f"ma-listener-{alias}",
            )
            info["thread"] = t
            t.start()

    def _stop_listener(self, alias: str) -> None:
        """Signal the listener thread to stop."""
        with self._lock:
            info = self._instances.get(alias)
            if info:
                info["stop"] = True

    def _listener_loop(self, alias: str, info: dict[str, Any],
                       ctx: PluginContext) -> None:
        """Background loop listening for events on a non-active account."""
        max_retries = 5
        base_delay = 10  # seconds
        max_delay = 300  # 5 minutes

        for attempt in range(max_retries + 1):
            try:
                from playerokapi.listener.listener import EventListener
                from playerokapi.enums import EventTypes

                account = info["account"]
                listener = EventListener(account)

                # Update status to online on successful connection
                accounts = load_accounts()
                for a in accounts:
                    if a["alias"] == alias:
                        a["status"] = "online"
                        a["error"] = None
                        break
                save_accounts(accounts)

                for event in listener.listen():
                    if info.get("stop"):
                        return
                    self._handle_non_active_event(alias, event, ctx)

                # If listen() ends normally without stop signal, treat as a crash
                if info.get("stop"):
                    return

            except Exception as exc:
                if info.get("stop"):
                    return

                LOGGER.error("multi_account: listener for %s crashed (attempt %d/%d): %s",
                             alias, attempt + 1, max_retries, exc)

                if attempt >= max_retries:
                    # All retries exhausted
                    error_msg = str(exc)[:200]
                    accounts = load_accounts()
                    for a in accounts:
                        if a["alias"] == alias:
                            a["status"] = "error"
                            a["error"] = error_msg
                            break
                    save_accounts(accounts)
                    _notify_admin(
                        ctx,
                        f"\U0001f534 Listener for '{alias}' stopped after "
                        f"{max_retries} retries.\nLast error: {error_msg}",
                        parse_mode=None,
                    )
                    return

                # Calculate backoff delay
                delay = min(base_delay * (2 ** attempt), max_delay)
                _notify_admin(
                    ctx,
                    f"\u26a0 Listener for '{alias}' crashed. "
                    f"Reconnecting in {delay}s (attempt {attempt + 1}/{max_retries})...",
                    parse_mode=None,
                )

                # Sleep with stop check
                for _ in range(int(delay)):
                    if info.get("stop"):
                        return
                    time.sleep(1)

    def _handle_non_active_event(self, alias: str, event: Any,
                                 ctx: PluginContext) -> None:
        """Send a notification for events from non-active accounts."""
        try:
            from playerokapi.enums import EventTypes

            etype = getattr(event, "type", None)
            if etype is None:
                return

            prefix = f"[{alias}]"
            msg = None

            if etype == EventTypes.NEW_MESSAGE:
                msg = f"{prefix} New message received"
            elif etype == EventTypes.NEW_DEAL:
                msg = f"{prefix} New deal created"
            elif etype == EventTypes.DEAL_CONFIRMED:
                msg = f"{prefix} Deal confirmed"
            elif etype == EventTypes.ITEM_PAID:
                msg = f"{prefix} Item paid"

            if msg:
                _notify_admin(ctx, msg, parse_mode=None)

        except Exception:
            LOGGER.debug("multi_account: event handling error for %s", alias, exc_info=True)


# --- Helpers ---------------------------------------------------------------

def _notify_admin(ctx: PluginContext, text: str, parse_mode: str | None = "Markdown") -> None:
    try:
        ctx.bot.send_message(ctx.admin_id, text, parse_mode=parse_mode)
    except Exception:
        try:
            ctx.bot.send_message(ctx.admin_id, text)
        except Exception:
            LOGGER.debug("multi_account: admin notify failed", exc_info=True)


def _send_or_edit(bot, chat_id: int, msg_id: int | None, text: str,
                  kb: Any = None, **kwargs) -> None:
    try:
        if msg_id:
            bot.edit_message_text(
                text, chat_id, msg_id, reply_markup=kb, **kwargs)
        else:
            bot.send_message(chat_id, text, reply_markup=kb, **kwargs)
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb, **kwargs)
        except Exception:
            pass


HANDLER = Handler()
