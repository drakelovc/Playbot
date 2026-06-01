"""Плагин autowithdraw -- автоматический вывод средств с баланса Playerok.

Поведение:
  * Фоновый поток каждые N минут проверяет баланс аккаунта.
  * Если balance.withdrawable >= (threshold + reserve) * 100 (API в копейках),
    вычисляет withdraw_amount = balance.withdrawable - reserve*100 и вызывает
    Account.request_withdrawal().
  * При событии DEAL_CONFIRMED / DEAL_CONFIRMED_AUTOMATICALLY планирует
    отложенную проверку через 30 секунд.
  * Поддерживаемые провайдеры: SBP, BANK_CARD_RU, USDT, YMONEY.
  * После 3 последовательных неудач отключает автовывод и уведомляет админа.

Telegram-команда -- /autowithdraw.
Хранилище: storage/plugins/autowithdraw/{config,history}.json.
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

LOGGER = logging.getLogger("playerok_bot.autowithdraw")
STORAGE_DIR = os.path.join("storage", "plugins", "autowithdraw")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")
PENDING_FILE = os.path.join(STORAGE_DIR, "pending.json")

PROVIDER_LABELS: dict[str, str] = {
    "SBP": "СБП",
    "BANK_CARD_RU": "Банковская карта",
    "USDT": "USDT (TRC20)",
    "YMONEY": "ЮMoney",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "auto_withdraw_enabled": False,
    "withdraw_threshold": 1000,
    "withdraw_provider": "SBP",
    "withdraw_account": "",
    "withdraw_reserve": 0,
    "withdraw_check_interval_minutes": 60,
    "withdraw_status_check_interval_minutes": 5,
    "sbp_bank_member_id": None,
    "payment_method_id": None,
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


# --- History helpers -------------------------------------------------------

def load_history() -> list[dict[str, Any]]:
    return common.load_json(HISTORY_FILE, [])


def save_history(history: list[dict[str, Any]]) -> None:
    common.save_json(HISTORY_FILE, history)


def add_history_entry(entry: dict[str, Any]) -> None:
    history = load_history()
    history.append(entry)
    if len(history) > 100:
        history = history[-100:]
    save_history(history)


# --- Pending helpers -------------------------------------------------------

def load_pending() -> list[dict[str, Any]]:
    return common.load_json(PENDING_FILE, [])


def save_pending(pending: list[dict[str, Any]]) -> None:
    # Cap at 50 entries to prevent unbounded growth
    if len(pending) > 50:
        pending = pending[-50:]
    common.save_json(PENDING_FILE, pending)


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="autowithdraw",
    name="Автовывод",
    icon="\U0001f4b8",
    description=(
        "Автоматический вывод средств с баланса Playerok при достижении порога. "
        "/autowithdraw в Telegram-боте для управления."
    ),
    instruction=(
        "*\U0001f4b8 Автовывод*\n\n"
        "*Что делает плагин:*\n"
        "- Периодически проверяет баланс Playerok.\n"
        "- Если сумма для вывода превышает порог, автоматически выводит средства "
        "на указанные реквизиты (СБП, карта, USDT, ЮMoney).\n"
        "- При подтверждении сделки делает дополнительную проверку через 30 сек.\n\n"
        "*Как настроить:*\n"
        "1. Включи плагин кнопкой ниже.\n"
        "2. `/autowithdraw` - задай провайдер, реквизиты, порог и резерв.\n"
        "3. Нажми «Включить автовывод».\n\n"
        "*Поддерживаемые провайдеры:*\n"
        "- СБП (номер телефона + ID банка)\n"
        "- Банковская карта РФ\n"
        "- USDT TRC20 (адрес кошелька)\n"
        "- ЮMoney (номер кошелька)"
    ),
    default_enabled=True,
    keywords=("вывод", "withdraw", "автовывод"),
)


# --- Helpers ---------------------------------------------------------------

def _notify_admin(ctx: PluginContext, text: str, parse_mode: str | None = "Markdown") -> None:
    try:
        ctx.bot.send_message(ctx.admin_id, text, parse_mode=parse_mode)
    except Exception:
        try:
            ctx.bot.send_message(ctx.admin_id, text)
        except Exception:
            ctx.log.debug("autowithdraw: admin notify failed", exc_info=True)


def _format_amount(kopeks: int) -> str:
    """Format kopeks as rubles string."""
    return f"{kopeks / 100:.2f}"


def _update_history_final_status(transaction_id: str, final_status: str) -> None:
    """Update the history entry matching transaction_id with a final_status."""
    history = load_history()
    for entry in history:
        if entry.get("transaction_id") == transaction_id:
            entry["final_status"] = final_status
            break
    save_history(history)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Main handler for the autowithdraw plugin."""

    _bg_thread: threading.Thread | None = None
    _bg_stop: bool = False
    _status_thread: threading.Thread | None = None
    _consecutive_failures: int = 0
    _withdraw_lock: threading.Lock = threading.Lock()

    def setup(self, ctx: PluginContext) -> None:
        get_config()  # ensure config file exists

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["autowithdraw"])
        def cmd_autowithdraw(message):
            if message.from_user.id != admin_id:
                return
            _send_main_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("aw:"))
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
                cfg["auto_withdraw_enabled"] = not cfg.get("auto_withdraw_enabled", False)
                save_config(cfg)
                if cfg["auto_withdraw_enabled"]:
                    self._consecutive_failures = 0
                _send_main_panel(bot, chat_id, msg_id)
            elif action == "set_threshold":
                wait_state[chat_id] = {"step": "wait_threshold"}
                bot.send_message(
                    chat_id,
                    "Введите порог для автовывода (в рублях).\n"
                    "Например: 1000\n\nОтмена: /cancel"
                )
            elif action == "set_reserve":
                wait_state[chat_id] = {"step": "wait_reserve"}
                bot.send_message(
                    chat_id,
                    "Введите сумму резерва (в рублях), которая останется на балансе.\n"
                    "Например: 500\n\nОтмена: /cancel"
                )
            elif action == "set_provider":
                kb = tg_types.InlineKeyboardMarkup()
                for pid, label in PROVIDER_LABELS.items():
                    kb.row(tg_types.InlineKeyboardButton(
                        label, callback_data=f"aw:prov:{pid}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 Назад", callback_data="aw:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f3e6 Выберите провайдер для вывода:", kb)
            elif action == "prov" and len(parts) >= 3:
                provider = parts[2]
                if provider in PROVIDER_LABELS:
                    cfg = get_config()
                    cfg["withdraw_provider"] = provider
                    save_config(cfg)
                    bot.send_message(
                        chat_id,
                        f"\u2705 Провайдер: {PROVIDER_LABELS[provider]}"
                    )
                _send_main_panel(bot, chat_id)
            elif action == "set_account":
                cfg = get_config()
                prov = cfg.get("withdraw_provider", "SBP")
                hint = "реквизиты"
                if prov == "SBP":
                    hint = "номер телефона (например +79001234567)"
                elif prov == "BANK_CARD_RU":
                    hint = "ID карты (из настроек Playerok)"
                elif prov == "USDT":
                    hint = "USDT TRC20 адрес"
                elif prov == "YMONEY":
                    hint = "номер кошелька ЮMoney"
                wait_state[chat_id] = {"step": "wait_account"}
                bot.send_message(
                    chat_id,
                    f"Введите {hint}:\n\nОтмена: /cancel"
                )
            elif action == "set_sbp_bank":
                wait_state[chat_id] = {"step": "wait_sbp_bank"}
                bot.send_message(
                    chat_id,
                    "Введите ID банка СБП (sbp_bank_member_id).\n"
                    "Его можно узнать из настроек вывода на Playerok.\n\n"
                    "Отмена: /cancel"
                )
            elif action == "set_payment_method":
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton(
                    "MIR", callback_data="aw:pm:MIR"))
                kb.row(tg_types.InlineKeyboardButton(
                    "VISA/Mastercard", callback_data="aw:pm:VISA_MASTERCARD"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u274c Не задано", callback_data="aw:pm:none"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u25c0 Назад", callback_data="aw:main"))
                _send_or_edit(bot, chat_id, msg_id,
                              "\U0001f4b3 Выберите платежный метод (для карты):", kb)
            elif action == "pm" and len(parts) >= 3:
                val = parts[2]
                cfg = get_config()
                cfg["payment_method_id"] = None if val == "none" else val
                save_config(cfg)
                bot.send_message(chat_id, f"\u2705 Платежный метод: {val}")
                _send_main_panel(bot, chat_id)
            elif action == "history":
                _send_history(bot, chat_id, msg_id)
            elif action == "pending":
                _send_pending(bot, chat_id, msg_id)
            elif action == "set_status_interval":
                wait_state[chat_id] = {"step": "wait_status_interval"}
                bot.send_message(
                    chat_id,
                    "Введите интервал проверки статусов выводов (в минутах).\n"
                    "Например: 5\n\nОтмена: /cancel"
                )
            elif action == "withdraw_now":
                threading.Thread(
                    target=self._do_withdraw,
                    args=(ctx,),
                    daemon=True,
                    name="aw-manual-withdraw",
                ).start()
                bot.send_message(chat_id, "\u23f3 Запрос на вывод отправлен...")

        @bot.message_handler(commands=["cancel"])
        def cancel_aw(message):
            if message.from_user.id != admin_id:
                return
            if message.chat.id in wait_state:
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, "Отменено.")

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
            content_types=["text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id, {})
            step = state.get("step")
            text = (message.text or "").strip()

            if step == "wait_threshold":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое положительное число.")
                    return
                cfg = get_config()
                cfg["withdraw_threshold"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Порог: {val} руб.")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_reserve":
                try:
                    val = int(text)
                    assert val >= 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое неотрицательное число.")
                    return
                cfg = get_config()
                cfg["withdraw_reserve"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Резерв: {val} руб.")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_account":
                if not text:
                    return
                cfg = get_config()
                cfg["withdraw_account"] = text
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Реквизиты: {text}")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_sbp_bank":
                if not text:
                    return
                cfg = get_config()
                cfg["sbp_bank_member_id"] = text
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 SBP bank ID: {text}")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_status_interval":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое положительное число.")
                    return
                cfg = get_config()
                cfg["withdraw_status_check_interval_minutes"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(
                    message.chat.id,
                    f"\u2705 Интервал проверки статусов: {val} мин."
                )
                _send_main_panel(bot, message.chat.id)

        def _send_main_panel(b, chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            enabled = cfg.get("auto_withdraw_enabled", False)
            status_icon = "\u2705" if enabled else "\u274c"
            status_text = "Включен" if enabled else "Выключен"
            provider = cfg.get("withdraw_provider", "SBP")
            provider_label = PROVIDER_LABELS.get(provider, provider)
            account = cfg.get("withdraw_account", "") or "не задано"
            threshold = cfg.get("withdraw_threshold", 1000)
            reserve = cfg.get("withdraw_reserve", 0)
            interval = cfg.get("withdraw_check_interval_minutes", 60)
            status_interval = cfg.get("withdraw_status_check_interval_minutes", 5)

            text = (
                "\U0001f4b8 *Автовывод*\n\n"
                f"Статус: {status_icon} {status_text}\n"
                f"Провайдер: {provider_label}\n"
                f"Реквизиты: `{common.md_escape(account)}`\n"
                f"Порог: {threshold} руб.\n"
                f"Резерв: {reserve} руб.\n"
                f"Интервал проверки: {interval} мин.\n"
                f"Интервал статусов: {status_interval} мин.\n"
            )
            if provider == "SBP":
                sbp_bank = cfg.get("sbp_bank_member_id") or "не задано"
                text += f"Банк СБП: `{common.md_escape(sbp_bank)}`\n"
            if provider == "BANK_CARD_RU":
                pm = cfg.get("payment_method_id") or "не задано"
                text += f"Платежный метод: `{common.md_escape(pm)}`\n"

            kb = tg_types.InlineKeyboardMarkup()
            toggle_text = "\u274c Выключить" if enabled else "\u2705 Включить"
            kb.row(tg_types.InlineKeyboardButton(
                toggle_text, callback_data="aw:toggle"))
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f4b0 Порог", callback_data="aw:set_threshold"),
                tg_types.InlineKeyboardButton(
                    "\U0001f4e6 Резерв", callback_data="aw:set_reserve"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\U0001f3e6 Провайдер", callback_data="aw:set_provider"),
                tg_types.InlineKeyboardButton(
                    "\U0001f4dd Реквизиты", callback_data="aw:set_account"),
            )
            if provider == "SBP":
                kb.row(tg_types.InlineKeyboardButton(
                    "\U0001f3e6 Банк СБП", callback_data="aw:set_sbp_bank"))
            if provider == "BANK_CARD_RU":
                kb.row(tg_types.InlineKeyboardButton(
                    "\U0001f4b3 Платежный метод", callback_data="aw:set_payment_method"))
            kb.row(tg_types.InlineKeyboardButton(
                "\U0001f4c3 История", callback_data="aw:history"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u23f3 Ожидают", callback_data="aw:pending"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u23f1 Интервал статусов", callback_data="aw:set_status_interval"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u26a1 Вывести сейчас", callback_data="aw:withdraw_now"))

            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_history(b, chat_id: int, edit_msg_id: int | None = None):
            history = load_history()
            if not history:
                text = "\U0001f4c3 *История выводов*\n\nПока пусто."
            else:
                lines = ["\U0001f4c3 *История выводов* (последние 20):\n"]
                for entry in history[-20:]:
                    ts = common.fmt_ts(entry.get("ts", 0))
                    amount = _format_amount(entry.get("amount", 0))
                    status = entry.get("status", "?")
                    provider = entry.get("provider", "?")
                    icon = "\u2705" if status == "success" else "\u274c"
                    lines.append(f"{icon} {ts} | {amount} \u20bd | {provider}")
                    if entry.get("error"):
                        lines.append(f"    \u26a0 {entry['error'][:60]}")
                text = "\n".join(lines)
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 Назад", callback_data="aw:main"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_pending(b, chat_id: int, edit_msg_id: int | None = None):
            pending = load_pending()
            if not pending:
                text = "\u23f3 *Ожидающие выводы*\n\nНет ожидающих выводов."
            else:
                lines = [f"\u23f3 *Ожидающие выводы* ({len(pending)}):\n"]
                for entry in pending:
                    created = common.fmt_ts(entry.get("created_at", 0))
                    amount = _format_amount(entry.get("amount", 0))
                    provider = entry.get("provider", "?")
                    entry_id = entry.get("id", "?")
                    lines.append(
                        f"\u2022 {created} | {amount} \u20bd | "
                        f"{PROVIDER_LABELS.get(provider, provider)}\n"
                        f"  ID: `{entry_id}`"
                    )
                text = "\n".join(lines)
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 Назад", callback_data="aw:main"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

    def start_background(self, ctx: PluginContext) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            pass
        else:
            self._bg_stop = False
            self._bg_thread = threading.Thread(
                target=self._withdraw_check_loop,
                args=(ctx,),
                daemon=True,
                name="aw-check-loop",
            )
            self._bg_thread.start()

        if self._status_thread is None or not self._status_thread.is_alive():
            self._status_thread = threading.Thread(
                target=self._status_check_loop,
                args=(ctx,),
                daemon=True,
                name="aw-status-loop",
            )
            self._status_thread.start()

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        deal_confirmed_auto = getattr(EventTypes, "DEAL_CONFIRMED_AUTOMATICALLY", None)
        if etype is EventTypes.DEAL_CONFIRMED or (
                deal_confirmed_auto is not None and etype is deal_confirmed_auto):
            threading.Thread(
                target=self._delayed_check,
                args=(ctx, 30),
                daemon=True,
                name="aw-delayed-check",
            ).start()
            return False  # don't claim exclusive handling
        return False

    # --- Internal ---------------------------------------------------------

    def _delayed_check(self, ctx: PluginContext, delay_seconds: int) -> None:
        time.sleep(delay_seconds)
        self._do_withdraw(ctx)

    def _withdraw_check_loop(self, ctx: PluginContext) -> None:
        while not self._bg_stop:
            cfg = get_config()
            interval = max(1, cfg.get("withdraw_check_interval_minutes", 60))
            try:
                if cfg.get("auto_withdraw_enabled", False):
                    self._do_withdraw(ctx)
            except Exception:
                ctx.log.exception("autowithdraw: error in check loop")
            # Sleep in small increments so we can stop quickly
            for _ in range(interval * 60):
                if self._bg_stop:
                    return
                time.sleep(1)

    def _status_check_loop(self, ctx: PluginContext) -> None:
        """Background thread: checks pending withdrawal statuses."""
        while not self._bg_stop:
            cfg = get_config()
            interval = max(1, cfg.get("withdraw_status_check_interval_minutes", 5))
            try:
                self._check_pending_statuses(ctx)
            except Exception:
                ctx.log.exception("autowithdraw: error in status check loop")
            # Sleep in small increments so we can stop quickly
            for _ in range(interval * 60):
                if self._bg_stop:
                    return
                time.sleep(1)

    def _check_pending_statuses(self, ctx: PluginContext) -> None:
        """Check statuses of pending withdrawals."""
        from playerokapi.enums import TransactionStatuses, TransactionOperations

        with self._withdraw_lock:
            pending = load_pending()
            if not pending:
                return

            # Clean up stale entries (older than 24 hours) before API call
            now_ts = common.now()
            non_stale: list[dict[str, Any]] = []
            stale_removed = False
            for entry in pending:
                entry_id = entry.get("id", "")
                created_at = entry.get("created_at", 0)
                if now_ts - created_at > 86400:
                    ctx.log.warning(
                        "autowithdraw: removing stale pending entry %s (older than 24h)",
                        entry_id,
                    )
                    stale_removed = True
                else:
                    non_stale.append(entry)

            if stale_removed:
                save_pending(non_stale)

            pending = non_stale
            if not pending:
                return

        # Fetch recent withdraw transactions (single API call)
        try:
            txn_list = ctx.playerok_acc.get_transactions(
                operation=TransactionOperations.WITHDRAW, count=24
            )
            transactions = getattr(txn_list, "transactions", []) or []
        except Exception as exc:
            ctx.log.error("autowithdraw: failed to fetch transactions: %s", exc)
            return

        # Build lookup by transaction ID
        txn_map: dict[str, Any] = {}
        for txn in transactions:
            txn_id = str(getattr(txn, "id", ""))
            if txn_id:
                txn_map[txn_id] = txn

        with self._withdraw_lock:
            # Re-load pending in case it changed while we were fetching
            pending = load_pending()
            updated = False
            remaining: list[dict[str, Any]] = []

            for entry in pending:
                entry_id = entry.get("id", "")

                txn = txn_map.get(entry_id)
                if txn is None:
                    remaining.append(entry)
                    continue

                status = getattr(txn, "status", None)

                if status == TransactionStatuses.CONFIRMED:
                    # Withdrawal credited
                    amount = entry.get("amount", 0)
                    provider = entry.get("provider", "?")
                    _notify_admin(
                        ctx,
                        f"\u2705 *Вывод зачислен*\n"
                        f"Сумма: {_format_amount(amount)} \u20bd\n"
                        f"Провайдер: {PROVIDER_LABELS.get(provider, provider)}\n"
                        f"ID: `{entry_id}`",
                    )
                    _update_history_final_status(entry_id, "credited")
                    updated = True

                elif status in (TransactionStatuses.FAILED, TransactionStatuses.ROLLED_BACK):
                    # Withdrawal rejected
                    amount = entry.get("amount", 0)
                    provider = entry.get("provider", "?")
                    status_desc = getattr(txn, "status_description", None) or ""
                    extra = f"\nПричина: {status_desc}" if status_desc else ""
                    _notify_admin(
                        ctx,
                        f"\u274c *Вывод отклонен*\n"
                        f"Сумма: {_format_amount(amount)} \u20bd\n"
                        f"Провайдер: {PROVIDER_LABELS.get(provider, provider)}\n"
                        f"ID: `{entry_id}`{extra}",
                    )
                    _update_history_final_status(entry_id, "rejected")
                    updated = True

                else:
                    # Still pending/processing
                    remaining.append(entry)

            if updated:
                save_pending(remaining)

    def _do_withdraw(self, ctx: PluginContext) -> None:
        """Check balance and withdraw if threshold exceeded."""
        with self._withdraw_lock:
            self._do_withdraw_locked(ctx)

    def _do_withdraw_locked(self, ctx: PluginContext) -> None:
        """Internal: runs under _withdraw_lock."""
        cfg = get_config()
        if not cfg.get("withdraw_account"):
            return

        if not ctx.playerok_acc:
            return

        try:
            ctx.playerok_acc.get()
        except Exception as exc:
            ctx.log.error("autowithdraw: failed to refresh account: %s", exc)
            return

        balance = getattr(ctx.playerok_acc, "balance", None)
        if balance is None:
            return

        withdrawable = getattr(balance, "withdrawable", 0) or 0
        threshold = int(cfg.get("withdraw_threshold", 1000))
        reserve = int(cfg.get("withdraw_reserve", 0))
        min_balance_kopeks = (threshold + reserve) * 100

        if withdrawable < min_balance_kopeks:
            return

        withdraw_amount = withdrawable - reserve * 100
        if withdraw_amount <= 0:
            return

        provider_name = cfg.get("withdraw_provider", "SBP")
        account = cfg.get("withdraw_account", "")
        sbp_bank_member_id = cfg.get("sbp_bank_member_id")
        payment_method_id_str = cfg.get("payment_method_id")

        try:
            from playerokapi.enums import TransactionProviderIds, TransactionPaymentMethodIds

            provider = getattr(TransactionProviderIds, provider_name)
            pm_id = None
            if payment_method_id_str and provider_name == "BANK_CARD_RU":
                pm_id = getattr(TransactionPaymentMethodIds, payment_method_id_str)

            result = ctx.playerok_acc.request_withdrawal(
                provider=provider,
                account=account,
                value=withdraw_amount,
                payment_method_id=pm_id,
                sbp_bank_member_id=sbp_bank_member_id if provider_name == "SBP" else None,
            )

            transaction_id = getattr(result, "id", None) or ""
            self._consecutive_failures = 0

            add_history_entry({
                "ts": common.now(),
                "amount": withdraw_amount,
                "provider": provider_name,
                "status": "success",
                "transaction_id": str(transaction_id),
            })

            # Track pending withdrawal for status monitoring
            if transaction_id:
                pending = load_pending()
                pending.append({
                    "id": str(transaction_id),
                    "amount": withdraw_amount,
                    "provider": provider_name,
                    "created_at": common.now(),
                })
                save_pending(pending)

            _notify_admin(
                ctx,
                f"\U0001f4b8 *Автовывод*: успешно\n"
                f"Сумма: {_format_amount(withdraw_amount)} \u20bd\n"
                f"Провайдер: {PROVIDER_LABELS.get(provider_name, provider_name)}\n"
                f"ID: `{transaction_id}`",
            )
            ctx.log.info("autowithdraw: success, amount=%d, txn=%s",
                         withdraw_amount, transaction_id)

        except Exception as exc:
            self._consecutive_failures += 1
            error_msg = str(exc)[:200]

            add_history_entry({
                "ts": common.now(),
                "amount": withdraw_amount,
                "provider": provider_name,
                "status": "error",
                "error": error_msg,
            })

            ctx.log.error("autowithdraw: withdrawal failed (%d/3): %s",
                          self._consecutive_failures, exc)

            if self._consecutive_failures >= 3:
                cfg = get_config()
                cfg["auto_withdraw_enabled"] = False
                save_config(cfg)
                _notify_admin(
                    ctx,
                    "\u274c *Автовывод ОТКЛЮЧЕН* после 3 неудачных попыток подряд.\n\n"
                    f"Последняя ошибка:\n`{common.md_escape(error_msg)}`\n\n"
                    "Проверьте настройки и включите снова через /autowithdraw",
                )
            else:
                _notify_admin(
                    ctx,
                    f"\u26a0 *Автовывод*: ошибка ({self._consecutive_failures}/3)\n"
                    f"Сумма: {_format_amount(withdraw_amount)} \u20bd\n"
                    f"Ошибка: `{common.md_escape(error_msg)}`",
                )


# --- Telegram helpers ------------------------------------------------------

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
