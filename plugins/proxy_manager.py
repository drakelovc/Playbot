"""Plugin proxy_manager -- multi-proxy management with health checks and rotation.

Features:
- Store multiple proxies with health status
- Periodic health checks (every 5 min)
- Manual and automatic rotation to next healthy proxy
- Telegram UI for management

Telegram command: /proxy_manager
Storage: storage/plugins/proxy_manager/proxies.json
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.proxy_manager")
STORAGE_DIR = os.path.join("storage", "plugins", "proxy_manager")
CONFIG_FILE = os.path.join(STORAGE_DIR, "proxies.json")

# Lock protecting concurrent read-modify-write on proxies.json
_proxies_lock = threading.Lock()

DEFAULT_CONFIG: list[dict[str, Any]] = []


def get_proxies() -> list[dict[str, Any]]:
    """Load proxies list from storage."""
    common.ensure_dir(STORAGE_DIR)
    proxies = common.load_json(CONFIG_FILE, None)
    if proxies is None:
        proxies = list(DEFAULT_CONFIG)
        common.save_json(CONFIG_FILE, proxies)
    return proxies


def save_proxies(proxies: list[dict[str, Any]]) -> None:
    common.ensure_dir(STORAGE_DIR)
    common.save_json(CONFIG_FILE, proxies)


def check_proxy_health(address: str, timeout: int = 10) -> tuple[bool, int | None]:
    """Check proxy health by requesting playerok.com. Returns (is_healthy, response_time_ms)."""
    if requests is None:
        return False, None
    proxy_url = f"http://{address}"
    try:
        start = time.time()
        resp = requests.get(
            "https://playerok.com",
            proxies={"https": proxy_url, "http": proxy_url},
            timeout=timeout,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        return resp.status_code < 500, elapsed_ms
    except Exception:
        return False, None


def get_next_healthy_proxy(proxies: list[dict[str, Any]], current_address: str | None = None) -> dict[str, Any] | None:
    """Get next healthy proxy after the current one (round-robin)."""
    healthy = [p for p in proxies if p.get("is_healthy", False)]
    if not healthy:
        return None
    if current_address is None:
        return healthy[0]
    # Find current index
    current_idx = -1
    for i, p in enumerate(healthy):
        if p.get("address") == current_address:
            current_idx = i
            break
    next_idx = (current_idx + 1) % len(healthy)
    return healthy[next_idx]


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="proxy_manager",
    name="\u041f\u0440\u043e\u043a\u0441\u0438-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440",
    icon="\U0001f310",
    description=(
        "\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u0438\u043c\u0438 \u043f\u0440\u043e\u043a\u0441\u0438 \u0441 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u043e\u0439 \u0437\u0434\u043e\u0440\u043e\u0432\u044c\u044f \u0438 \u0430\u0432\u0442\u043e-\u0440\u043e\u0442\u0430\u0446\u0438\u0435\u0439. "
        "/proxy_manager \u0434\u043b\u044f \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    ),
    instruction=(
        "*\U0001f310 \u041f\u0440\u043e\u043a\u0441\u0438-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u0425\u0440\u0430\u043d\u0438\u0442 \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u043e\u043a\u0441\u0438 \u0441 \u043e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u043d\u0438\u0435\u043c \u0441\u0442\u0430\u0442\u0443\u0441\u0430.\n"
        "- \u041f\u0435\u0440\u0438\u043e\u0434\u0438\u0447\u0435\u0441\u043a\u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e\u0441\u0442\u044c \u043f\u0440\u043e\u043a\u0441\u0438.\n"
        "- \u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0435\u0442 \u043d\u0430 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0437\u0434\u043e\u0440\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u043a\u0441\u0438.\n\n"
        "*\u041a\u0430\u043a \u043d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c:*\n"
        "1. `/proxy_manager` - \u043e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c.\n"
        "2. \u0414\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043f\u0440\u043e\u043a\u0441\u0438 (\u0444\u043e\u0440\u043c\u0430\u0442: user:pass@ip:port).\n"
        "3. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0437\u0434\u043e\u0440\u043e\u0432\u044c\u0435 \u0438 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u0443\u0439\u0442\u0435."
    ),
    default_enabled=True,
    keywords=("\u043f\u0440\u043e\u043a\u0441\u0438", "proxy", "\u0440\u043e\u0442\u0430\u0446\u0438\u044f"),
)


class Handler:
    """Main handler for the proxy_manager plugin."""

    _bg_thread: threading.Thread | None = None
    _bg_stop: bool = False

    def setup(self, ctx: PluginContext) -> None:
        get_proxies()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["proxy_manager"])
        def cmd_proxy_manager(message):
            if message.from_user.id != admin_id:
                return
            _send_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:"))
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
                wait_state[chat_id] = {"step": "wait_address"}
                bot.send_message(
                    chat_id,
                    "\U0001f310 \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0430\u0434\u0440\u0435\u0441 \u043f\u0440\u043e\u043a\u0441\u0438 (user:pass@ip:port):\n\n\u041e\u0442\u043c\u0435\u043d\u0430: /cancel"
                )
            elif action == "del":
                proxies = get_proxies()
                if not proxies:
                    bot.send_message(chat_id, "\u041d\u0435\u0442 \u043f\u0440\u043e\u043a\u0441\u0438.")
                    return
                kb = tg_types.InlineKeyboardMarkup()
                for i, p in enumerate(proxies):
                    label = p.get("label") or p.get("address", "?")[:25]
                    kb.row(tg_types.InlineKeyboardButton(
                        f"\u274c {label}", callback_data=f"pm:rm:{i}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="pm:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f5d1 \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043f\u0440\u043e\u043a\u0441\u0438 \u0434\u043b\u044f \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u044f:", kb)
            elif action == "rm" and len(parts) >= 3:
                try:
                    idx = int(parts[2])
                    with _proxies_lock:
                        proxies = get_proxies()
                        if 0 <= idx < len(proxies):
                            removed = proxies.pop(idx)
                            save_proxies(proxies)
                            bot.send_message(chat_id,
                                             f"\u2705 \u0423\u0434\u0430\u043b\u0435\u043d: {removed.get('address', '?')}")
                except (ValueError, IndexError):
                    pass
                _send_panel(bot, chat_id)
            elif action == "check":
                bot.send_message(chat_id, "\u23f3 \u041f\u0440\u043e\u0432\u0435\u0440\u044f\u044e \u0432\u0441\u0435 \u043f\u0440\u043e\u043a\u0441\u0438...")
                threading.Thread(
                    target=self._check_all_and_notify,
                    args=(ctx,),
                    daemon=True,
                ).start()
            elif action == "rotate":
                self._rotate_proxy(ctx)
                _send_panel(bot, chat_id)

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

            if step == "wait_address":
                if not text:
                    bot.send_message(message.chat.id, "\u041d\u0443\u0436\u043d\u043e \u0432\u0432\u0435\u0441\u0442\u0438 \u0430\u0434\u0440\u0435\u0441.")
                    return
                # Clean up http:// prefix if present
                addr = text.replace("https://", "").replace("http://", "").strip()
                with _proxies_lock:
                    proxies = get_proxies()
                    proxies.append({
                        "address": addr,
                        "label": addr.split("@")[-1] if "@" in addr else addr,
                        "is_active": len(proxies) == 0,
                        "last_check": None,
                        "is_healthy": False,
                        "response_time_ms": None,
                    })
                    save_proxies(proxies)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 \u041f\u0440\u043e\u043a\u0441\u0438 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d: {addr}")
                _send_panel(bot, message.chat.id)

        def _send_panel(b, chat_id: int, edit_msg_id: int | None = None):
            proxies = get_proxies()
            if not proxies:
                text = "\U0001f310 *\u041f\u0440\u043e\u043a\u0441\u0438-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440*\n\n\u041d\u0435\u0442 \u043f\u0440\u043e\u043a\u0441\u0438."
            else:
                lines = ["\U0001f310 *\u041f\u0440\u043e\u043a\u0441\u0438-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440*\n"]
                for p in proxies:
                    health_icon = "\U0001f7e2" if p.get("is_healthy") else "\U0001f534"
                    active_icon = "\u27a1\ufe0f" if p.get("is_active") else "  "
                    label = p.get("label") or p.get("address", "?")[:25]
                    rt = p.get("response_time_ms")
                    rt_str = f" ({rt}ms)" if rt is not None else ""
                    lines.append(f"{active_icon}{health_icon} `{label}`{rt_str}")
                text = "\n".join(lines)

            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c", callback_data="pm:add"),
                tg_types.InlineKeyboardButton(
                    "\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data="pm:del"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f50d \u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0432\u0441\u0435", callback_data="pm:check"),
                tg_types.InlineKeyboardButton(
                    "\U0001f504 \u0420\u043e\u0442\u0430\u0446\u0438\u044f", callback_data="pm:rotate"),
            )
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def start_background(self, ctx: PluginContext) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_stop = False
        self._bg_thread = threading.Thread(
            target=self._health_check_loop,
            args=(ctx,),
            daemon=True,
            name="pm-health-loop",
        )
        self._bg_thread.start()

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        return False

    # --- Internal ---------------------------------------------------------

    def _health_check_loop(self, ctx: PluginContext) -> None:
        """Check all proxies health every 5 minutes."""
        while not self._bg_stop:
            try:
                self._check_all(ctx)
            except Exception:
                LOGGER.exception("proxy_manager: health check loop error")
            # Sleep 5 minutes in small increments
            for _ in range(300):
                if self._bg_stop:
                    return
                time.sleep(1)

    def _check_all(self, ctx: PluginContext) -> None:
        """Check health of all proxies."""
        with _proxies_lock:
            proxies = get_proxies()
            changed = False
            for p in proxies:
                addr = p.get("address", "")
                if not addr:
                    continue
                is_healthy, response_time = check_proxy_health(addr)
                p["is_healthy"] = is_healthy
                p["response_time_ms"] = response_time
                p["last_check"] = time.time()
                changed = True
            if changed:
                save_proxies(proxies)

    def _check_all_and_notify(self, ctx: PluginContext) -> None:
        """Check all proxies and notify admin."""
        self._check_all(ctx)
        proxies = get_proxies()
        healthy_count = sum(1 for p in proxies if p.get("is_healthy"))
        try:
            ctx.bot.send_message(
                ctx.admin_id,
                f"\U0001f310 \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430: {healthy_count}/{len(proxies)} \u0437\u0434\u043e\u0440\u043e\u0432\u044b\u0445",
            )
        except Exception:
            pass

    def _rotate_proxy(self, ctx: PluginContext) -> None:
        """Rotate to next healthy proxy and update config."""
        with _proxies_lock:
            proxies = get_proxies()
            # Find currently active proxy
            current_addr = None
            for p in proxies:
                if p.get("is_active"):
                    current_addr = p.get("address")
                    break

            next_proxy = get_next_healthy_proxy(proxies, current_addr)
            if next_proxy is None:
                try:
                    ctx.bot.send_message(ctx.admin_id,
                                         "\u274c \u041d\u0435\u0442 \u0437\u0434\u043e\u0440\u043e\u0432\u044b\u0445 \u043f\u0440\u043e\u043a\u0441\u0438 \u0434\u043b\u044f \u0440\u043e\u0442\u0430\u0446\u0438\u0438.")
                except Exception:
                    pass
                return

            # Update active status
            for p in proxies:
                p["is_active"] = (p.get("address") == next_proxy.get("address"))
            save_proxies(proxies)

        # Update main config proxy (outside lock since it uses different storage)
        try:
            cfg = ctx.get_config()
            cfg["playerok_proxy"] = next_proxy["address"]
            ctx.save_config(cfg)
            LOGGER.info("proxy_manager: rotated to %s", next_proxy["address"])
        except Exception as exc:
            LOGGER.error("proxy_manager: rotate config update failed: %s", exc)

        try:
            ctx.bot.send_message(
                ctx.admin_id,
                f"\U0001f504 \u041f\u0440\u043e\u043a\u0441\u0438 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0435\u043d \u043d\u0430: `{next_proxy['address']}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass


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
