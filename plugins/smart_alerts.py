"""Plugin smart_alerts -- мониторинг аномалий продаж, баланса и подозрительной активности.

Поведение:
  * Фоновый поток каждые N минут проверяет метрики магазина.
  * Отслеживает продажи, возвраты, проблемы и ошибки за 24ч.
  * При аномалиях отправляет алерт админу в Telegram.
  * Поддерживает кулдаун и тихие часы.

Telegram-команда -- /alerts.
Хранилище: storage/plugins/smart_alerts/{config,events,cooldowns}.json.
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

LOGGER = logging.getLogger("playerok_bot.smart_alerts")
STORAGE_DIR = os.path.join("storage", "plugins", "smart_alerts")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
EVENTS_FILE = os.path.join(STORAGE_DIR, "events.json")
COOLDOWNS_FILE = os.path.join(STORAGE_DIR, "cooldowns.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "min_balance_rub": 500,
    "expected_sales_per_hour": 2.0,
    "max_refunds_per_day": 3,
    "rapid_buyer_threshold": 3,
    "check_interval_minutes": 5,
    "alert_cooldown_minutes": 30,
    "quiet_hours_start": None,
    "quiet_hours_end": None,
    "zero_sales_hours": 6,
    "enabled_alert_types": {
        "sales_anomaly": True,
        "balance": True,
        "suspicious_activity": True,
        "listing_issues": True,
        "system_health": True,
    },
}


# --- Config helpers --------------------------------------------------------

def get_config() -> dict[str, Any]:
    cfg = common.load_json(CONFIG_FILE, None)
    changed = False
    if cfg is None:
        cfg = dict(DEFAULT_CONFIG)
        cfg["enabled_alert_types"] = dict(DEFAULT_CONFIG["enabled_alert_types"])
        changed = True
    else:
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v if not isinstance(v, dict) else dict(v)
                changed = True
        # Ensure all alert type keys exist
        if isinstance(cfg.get("enabled_alert_types"), dict):
            for ak, av in DEFAULT_CONFIG["enabled_alert_types"].items():
                if ak not in cfg["enabled_alert_types"]:
                    cfg["enabled_alert_types"][ak] = av
                    changed = True
        else:
            cfg["enabled_alert_types"] = dict(DEFAULT_CONFIG["enabled_alert_types"])
            changed = True
    if changed:
        common.save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    common.save_json(CONFIG_FILE, cfg)


# --- Event store -----------------------------------------------------------

_events_lock = threading.Lock()


def load_events() -> list[dict[str, Any]]:
    return common.load_json(EVENTS_FILE, [])


def save_events(events: list[dict[str, Any]]) -> None:
    common.save_json(EVENTS_FILE, events)


def add_event(event: dict[str, Any]) -> None:
    with _events_lock:
        events = load_events()
        events.append(event)
        save_events(events)


def trim_events() -> None:
    """Remove events older than 24 hours."""
    cutoff = common.now() - 86400
    with _events_lock:
        events = load_events()
        events = [e for e in events if e.get("ts", 0) >= cutoff]
        save_events(events)


# --- Cooldown system -------------------------------------------------------

_cooldowns: dict[str, int] = {}
_cooldowns_lock = threading.Lock()


def _load_cooldowns() -> dict[str, int]:
    global _cooldowns
    data = common.load_json(COOLDOWNS_FILE, {})
    if isinstance(data, dict):
        _cooldowns = data
    return _cooldowns


def _save_cooldowns() -> None:
    common.save_json(COOLDOWNS_FILE, _cooldowns)


def _can_fire_alert(alert_key: str, cfg: dict[str, Any]) -> bool:
    """Check if alert can fire (respects cooldown and quiet hours)."""
    # Check quiet hours
    # Uses server local time (TZ env var)
    qstart = cfg.get("quiet_hours_start")
    qend = cfg.get("quiet_hours_end")
    if qstart is not None and qend is not None:
        if qstart == qend:
            # start == end means 24-hour suppression
            return False
        current_hour = time.localtime().tm_hour
        if qstart < qend:
            if qstart <= current_hour < qend:
                return False
        elif qstart > qend:
            # Overnight range (e.g. 23-7)
            if current_hour >= qstart or current_hour < qend:
                return False

    # Check cooldown
    cooldown_minutes = cfg.get("alert_cooldown_minutes", 30)
    with _cooldowns_lock:
        _load_cooldowns()
        last_fired = _cooldowns.get(alert_key, 0)
    if common.now() - last_fired < cooldown_minutes * 60:
        return False

    return True


def _mark_fired(alert_key: str) -> None:
    with _cooldowns_lock:
        _load_cooldowns()
        _cooldowns[alert_key] = common.now()
        _save_cooldowns()


# --- Alert sending ---------------------------------------------------------

def _send_alert(ctx: PluginContext, alert_key: str, text: str) -> bool:
    """Send alert if cooldown allows. Returns True if sent."""
    cfg = get_config()
    if not cfg.get("enabled", True):
        return False
    if not _can_fire_alert(alert_key, cfg):
        return False
    _mark_fired(alert_key)
    try:
        ctx.bot.send_message(ctx.admin_id, text, parse_mode="Markdown")
    except Exception:
        try:
            ctx.bot.send_message(ctx.admin_id, text)
        except Exception:
            ctx.log.debug("smart_alerts: admin notify failed", exc_info=True)
    return True


# --- Checks ----------------------------------------------------------------

def _run_checks(ctx: PluginContext) -> None:
    """Run all enabled alert checks."""
    cfg = get_config()
    if not cfg.get("enabled", True):
        return

    trim_events()
    alert_types = cfg.get("enabled_alert_types", {})
    now_ts = common.now()

    events = load_events()

    # 1. Sales anomaly
    if alert_types.get("sales_anomaly", True):
        _check_sales_anomaly(ctx, cfg, events, now_ts)

    # 2. Balance
    if alert_types.get("balance", True):
        _check_balance(ctx, cfg)

    # 3. Suspicious activity
    if alert_types.get("suspicious_activity", True):
        _check_suspicious_activity(ctx, cfg, events, now_ts)

    # 4. System health
    if alert_types.get("system_health", True):
        _check_system_health(ctx, cfg, events, now_ts)

    # 5. Listing issues - placeholder for future implementation.
    # Not shown in the /alerts UI panel (absent from type_labels dict)
    # because the check is not yet implemented.


def _check_sales_anomaly(ctx: PluginContext, cfg: dict, events: list, now_ts: int) -> None:
    zero_hours = cfg.get("zero_sales_hours", 6)
    expected = cfg.get("expected_sales_per_hour", 2.0)

    # Count sales in last zero_hours
    cutoff = now_ts - zero_hours * 3600
    sales_in_window = [e for e in events if e.get("type") == "sale" and e.get("ts", 0) >= cutoff]

    if len(sales_in_window) == 0 and expected > 0:
        _send_alert(
            ctx, "sales_zero",
            "\U0001f6a8 *Нет продаж*\n\n"
            f"За последние {zero_hours} ч. "
            "не было ни одной продажи."
        )

    # Check for spike in last hour
    hour_cutoff = now_ts - 3600
    sales_last_hour = [e for e in events if e.get("type") == "sale" and e.get("ts", 0) >= hour_cutoff]
    if expected > 0 and len(sales_last_hour) > 3 * expected:
        _send_alert(
            ctx, "sales_spike",
            "\u26a1 *Всплеск продаж*\n\n"
            f"За последний час: {len(sales_last_hour)} "
            f"(ожидаемо: {expected:.1f})"
        )


def _check_balance(ctx: PluginContext, cfg: dict) -> None:
    if not ctx.playerok_acc:
        return
    try:
        balance = getattr(ctx.playerok_acc, "balance", None)
        if balance is None:
            return
        withdrawable = getattr(balance, "withdrawable", 0) or 0
    except Exception:
        return

    min_balance_kopeks = cfg.get("min_balance_rub", 500) * 100
    if withdrawable < min_balance_kopeks:
        min_bal = cfg.get("min_balance_rub", 500)
        _send_alert(
            ctx, "balance_low",
            "\U0001f4b0 *Низкий баланс*\n\n"
            f"Баланс: {withdrawable / 100:.2f} \u20bd\n"
            f"Порог: {min_bal} \u20bd"
        )


def _check_suspicious_activity(ctx: PluginContext, cfg: dict, events: list, now_ts: int) -> None:
    max_refunds = cfg.get("max_refunds_per_day", 3)
    rapid_threshold = cfg.get("rapid_buyer_threshold", 3)

    # Count refunds in last 24h
    day_cutoff = now_ts - 86400
    refunds = [e for e in events if e.get("type") == "refund" and e.get("ts", 0) >= day_cutoff]
    if len(refunds) > max_refunds:
        _send_alert(
            ctx, "refunds_high",
            "\u26a0 *Много возвратов*\n\n"
            f"Возвратов за 24ч: {len(refunds)} "
            f"(порог: {max_refunds})"
        )

    # Check rapid buyer in last hour
    hour_cutoff = now_ts - 3600
    sales_last_hour = [e for e in events if e.get("type") == "sale" and e.get("ts", 0) >= hour_cutoff]
    buyer_counts: dict[str, int] = {}
    for s in sales_last_hour:
        bid = s.get("buyer_id", "")
        if bid:
            buyer_counts[bid] = buyer_counts.get(bid, 0) + 1
    for bid, count in buyer_counts.items():
        if count >= rapid_threshold:
            _send_alert(
                ctx, "rapid_buyer",
                "\U0001f575 *Быстрый покупатель*\n\n"
                f"Покупатель `{bid}` "
                f"сделал {count} покупок за 1ч."
            )
            break  # Only one alert per check cycle


def _check_system_health(ctx: PluginContext, cfg: dict, events: list, now_ts: int) -> None:
    hour_cutoff = now_ts - 3600
    errors = [e for e in events if e.get("type") == "error" and e.get("ts", 0) >= hour_cutoff]
    if len(errors) > 5:
        _send_alert(
            ctx, "errors_high",
            "\U0001f6a8 *Много ошибок*\n\n"
            f"Ошибок за последний час: {len(errors)}"
        )


# --- Telegram helpers ------------------------------------------------------

def _send_or_edit(bot, chat_id: int, msg_id: int | None, text: str,
                  kb=None, parse_mode: str | None = "Markdown") -> None:
    if msg_id:
        try:
            bot.edit_message_text(
                text, chat_id, msg_id, reply_markup=kb, parse_mode=parse_mode)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb, parse_mode=parse_mode)


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="smart_alerts",
    name="Умные алерты",
    icon="\U0001f514",
    description=(
        "Мониторинг аномалий продаж, баланса и "
        "подозрительной активности. /alerts для настройки."
    ),
    instruction=(
        "*\U0001f514 Умные алерты*\n\n"
        "*Что делает плагин:*\n"
        "- Периодически проверяет метрики магазина.\n"
        "- Отправляет алерты при аномалиях.\n\n"
        "*Как настроить:*\n"
        "1. Включи плагин.\n"
        "2. `/alerts` - настрой пороги и типы алертов.\n"
        "3. Бот будет мониторить автоматически."
    ),
    default_enabled=True,
    keywords=("алерты", "alerts", "мониторинг", "аномалии"),
)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Main handler for the smart_alerts plugin."""

    def __init__(self) -> None:
        self._bg_thread: threading.Thread | None = None
        self._bg_stop: bool = False

    def setup(self, ctx: PluginContext) -> None:
        get_config()  # ensure config file exists

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        if etype is EventTypes.ITEM_PAID:
            buyer_id = getattr(event, "buyer_id", None) or getattr(event, "user_id", "")
            add_event({"type": "sale", "ts": common.now(), "buyer_id": buyer_id or ""})
        elif etype is EventTypes.DEAL_HAS_PROBLEM:
            deal_id = getattr(event, "id", "")
            add_event({"type": "problem", "ts": common.now(), "deal_id": deal_id})
        elif etype is EventTypes.DEAL_ROLLED_BACK:
            deal_id = getattr(event, "id", "")
            add_event({"type": "refund", "ts": common.now(), "deal_id": deal_id})

        return False  # don't claim exclusive handling

    def start_background(self, ctx: PluginContext) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_stop = False
        self._bg_thread = threading.Thread(
            target=self._check_loop,
            args=(ctx,),
            daemon=True,
            name="sa-check-loop",
        )
        self._bg_thread.start()

    def _check_loop(self, ctx: PluginContext) -> None:
        while not self._bg_stop:
            cfg = get_config()
            interval = max(1, cfg.get("check_interval_minutes", 5))
            try:
                if cfg.get("enabled", True):
                    _run_checks(ctx)
            except Exception:
                ctx.log.exception("smart_alerts: error in check loop")
            # Sleep in small increments so we can stop quickly
            for _ in range(interval * 60):
                if self._bg_stop:
                    return
                time.sleep(1)

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["alerts"])
        def cmd_alerts(message):
            if message.from_user.id != admin_id:
                return
            _send_main_panel(bot, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sa:"))
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
                cfg["enabled"] = not cfg.get("enabled", True)
                save_config(cfg)
                _send_main_panel(bot, chat_id, msg_id)
            elif action == "set_balance":
                wait_state[chat_id] = {"step": "wait_balance"}
                bot.send_message(
                    chat_id,
                    "Введите минимальный баланс (в рублях).\n"
                    "Например: 500\n\nОтмена: /cancel"
                )
            elif action == "set_sales":
                wait_state[chat_id] = {"step": "wait_sales"}
                bot.send_message(
                    chat_id,
                    "Введите ожидаемое кол-во продаж в час.\n"
                    "Например: 2.0\n\nОтмена: /cancel"
                )
            elif action == "set_refunds":
                wait_state[chat_id] = {"step": "wait_refunds"}
                bot.send_message(
                    chat_id,
                    "Введите макс. возвратов в день.\n"
                    "Например: 3\n\nОтмена: /cancel"
                )
            elif action == "set_cooldown":
                wait_state[chat_id] = {"step": "wait_cooldown"}
                bot.send_message(
                    chat_id,
                    "Введите кулдаун алертов (в минутах).\n"
                    "Например: 30\n\nОтмена: /cancel"
                )
            elif action == "set_quiet":
                wait_state[chat_id] = {"step": "wait_quiet"}
                bot.send_message(
                    chat_id,
                    "Введите тихие часы в формате HH-HH (0-23).\n"
                    "Например: 23-7\n"
                    "Для отключения: off\n\nОтмена: /cancel"
                )
            elif action.startswith("ta_"):
                # Toggle alert type
                atype = action[3:]
                cfg = get_config()
                at = cfg.get("enabled_alert_types", {})
                if atype in at:
                    at[atype] = not at[atype]
                    cfg["enabled_alert_types"] = at
                    save_config(cfg)
                _send_main_panel(bot, chat_id, msg_id)
            elif action == "status":
                _send_status(bot, chat_id, msg_id)

        @bot.message_handler(commands=["cancel"])
        def cancel_sa(message):
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

            if step == "wait_balance":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое положительное число.")
                    return
                cfg = get_config()
                cfg["min_balance_rub"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Мин. баланс: {val} руб.")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_sales":
                try:
                    val = float(text)
                    assert val >= 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно число >= 0.")
                    return
                cfg = get_config()
                cfg["expected_sales_per_hour"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Ожидаемо продаж/час: {val}")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_refunds":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое положительное число.")
                    return
                cfg = get_config()
                cfg["max_refunds_per_day"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Макс. возвратов/день: {val}")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_cooldown":
                try:
                    val = int(text)
                    assert val > 0
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Нужно целое положительное число.")
                    return
                cfg = get_config()
                cfg["alert_cooldown_minutes"] = val
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Кулдаун: {val} мин.")
                _send_main_panel(bot, message.chat.id)

            elif step == "wait_quiet":
                if text.lower() == "off":
                    cfg = get_config()
                    cfg["quiet_hours_start"] = None
                    cfg["quiet_hours_end"] = None
                    save_config(cfg)
                    wait_state.pop(message.chat.id, None)
                    bot.send_message(message.chat.id, "\u2705 Тихие часы отключены.")
                    _send_main_panel(bot, message.chat.id)
                    return
                parts = text.split("-")
                if len(parts) != 2:
                    bot.send_message(message.chat.id, "Формат: HH-HH или off")
                    return
                try:
                    start_h = int(parts[0].strip())
                    end_h = int(parts[1].strip())
                    assert 0 <= start_h <= 23 and 0 <= end_h <= 23
                except (ValueError, AssertionError):
                    bot.send_message(message.chat.id, "Часы 0-23. Формат: HH-HH")
                    return
                cfg = get_config()
                cfg["quiet_hours_start"] = start_h
                cfg["quiet_hours_end"] = end_h
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"\u2705 Тихие часы: {start_h}-{end_h}")
                _send_main_panel(bot, message.chat.id)

        def _send_main_panel(b, chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            enabled = cfg.get("enabled", True)
            status_icon = "\u2705" if enabled else "\u274c"
            status_text = "Включен" if enabled else "Выключен"
            min_bal = cfg.get("min_balance_rub", 500)
            sales_h = cfg.get("expected_sales_per_hour", 2.0)
            max_ref = cfg.get("max_refunds_per_day", 3)
            cooldown = cfg.get("alert_cooldown_minutes", 30)
            qs = cfg.get("quiet_hours_start")
            qe = cfg.get("quiet_hours_end")
            quiet_str = f"{qs}-{qe}" if qs is not None and qe is not None else "откл."
            at = cfg.get("enabled_alert_types", {})

            text = (
                "\U0001f514 *Умные алерты*\n\n"
                f"Статус: {status_icon} {status_text}\n"
                f"Мин. баланс: {min_bal} руб.\n"
                f"Продаж/час: {sales_h}\n"
                f"Макс. возвратов/день: {max_ref}\n"
                f"Кулдаун: {cooldown} мин.\n"
                f"Тихие часы: {quiet_str}\n"
            )

            kb = tg_types.InlineKeyboardMarkup()
            toggle_text = "\u274c Выключить" if enabled else "\u2705 Включить"
            kb.row(tg_types.InlineKeyboardButton(toggle_text, callback_data="sa:toggle"))
            kb.row(
                tg_types.InlineKeyboardButton("\U0001f4b0 Мин. баланс", callback_data="sa:set_balance"),
                tg_types.InlineKeyboardButton("\U0001f4c8 Продаж/ч", callback_data="sa:set_sales"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("\u21a9 Возвраты", callback_data="sa:set_refunds"),
                tg_types.InlineKeyboardButton("\u23f1 Кулдаун", callback_data="sa:set_cooldown"),
            )
            kb.row(tg_types.InlineKeyboardButton("\U0001f319 Тихие часы", callback_data="sa:set_quiet"))
            # Alert type toggles
            type_labels = {
                "sales_anomaly": "Продажи",
                "balance": "Баланс",
                "suspicious_activity": "Подозр.",
                "system_health": "Здоровье",
            }
            for atype, label in type_labels.items():
                on = at.get(atype, True)
                icon = "\u2705" if on else "\u274c"
                kb.row(tg_types.InlineKeyboardButton(
                    f"{icon} {label}", callback_data=f"sa:ta_{atype}"))
            kb.row(tg_types.InlineKeyboardButton("\U0001f4ca Статус", callback_data="sa:status"))

            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_status(b, chat_id: int, edit_msg_id: int | None = None):
            events = load_events()
            sales = sum(1 for e in events if e.get("type") == "sale")
            refunds = sum(1 for e in events if e.get("type") == "refund")
            problems = sum(1 for e in events if e.get("type") == "problem")
            errors = sum(1 for e in events if e.get("type") == "error")
            text = (
                "\U0001f4ca *Статус мониторинга* (за 24ч)\n\n"
                f"Продаж: {sales}\n"
                f"Возвратов: {refunds}\n"
                f"Проблем: {problems}\n"
                f"Ошибок: {errors}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("\u25c0 Назад", callback_data="sa:main"))
            _send_or_edit(b, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")


HANDLER = Handler()
