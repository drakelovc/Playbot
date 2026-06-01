"""Плагин autosteamrental — автоматическая аренда Steam-аккаунтов на Playerok.

Поведение:
  * Покупатель оплачивает товар-аренду → плагин берёт свободный аккаунт из
    пула, отправляет в чат Playerok логин/пароль и время аренды.
  * Покупатель пишет `!steamguard <ник>` → плагин запускает таймер аренды (с
    момента первого получения кода) и шлёт ему Steam Guard-код.
  * Фоновая задача отслеживает истечение аренды: за N минут до конца — шлёт
    напоминание, после истечения — шлёт сообщение об окончании и закрывает
    выдачу (`status = expired`).
  * Длительность аренды берётся либо из `lot_map[item_id]`, либо парсится из
    названия товара (`на 2 часа`, `30 минут`, `1 сутки` и т. п.).

Telegram-команда — `/autosteamrental`. Хранилище плагина:
`storage/plugins/autosteamrental/{accounts,assignments,history,events,config}.json`.
Не пересекается с autosteamoffline.
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

try:
    from . import _steam_session as steam_session
except Exception:  # pragma: no cover — на случай отсутствия rsa / steampy
    steam_session = None  # type: ignore

LOGGER = logging.getLogger("playerok_bot.autosteamrental")
STORAGE_DIR = os.path.join("storage", "plugins", "autosteamrental")
ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")
ASSIGNMENTS_FILE = os.path.join(STORAGE_DIR, "assignments.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")
EVENTS_FILE = os.path.join(STORAGE_DIR, "events.json")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")


# ─── Шаблоны ─────────────────────────────────────────────────────

DEFAULT_TEMPLATES: dict[str, str] = {
    "issue": (
        "👤 Спасибо за аренду аккаунта Steam!\n\n"
        "Ваши данные:\n"
        "• Логин: {login}\n"
        "• Пароль: {password}\n"
        "• Время аренды: {duration}\n\n"
        "Войдите в аккаунт, а после напишите команду\n"
        "!steamguard {login}, чтобы получить код из SteamGuard. "
        "Будьте внимательны, код действует только 30 секунд.\n"
        "Срок аренды начнётся с момента первого получения кода\n\n"
        "💡 Если нужна помощь, позовите продавца командой !продавец, и он вам поможет"
    ),
    "guard_code_first": (
        "🔐 Код SteamGuard: {code}\n"
        "Используйте его, чтобы войти в аккаунт Steam\n\n"
        "🕒 Аренда началась!\n"
        "• Начало: {started_at} (МСК)\n"
        "• Окончание: {ends_at} (МСК)\n\n"
        "Бот предупредит вас за 15 минут до окончания\n"
        "После окончания аренды, ваша сессия закроется, а данные от аккаунта сменятся\n\n"
        "🎮 Приятной игры!\n\n"
        "💡 Если нужна помощь, позовите продавца командой !продавец, и он вам поможет"
    ),
    "guard_code": (
        "🔐 Код SteamGuard: {code}\n"
        "Используйте его, чтобы войти в аккаунт Steam\n"
        "⏱ До окончания аренды: {time_left}"
    ),
    "expired": (
        "📢 Срок аренды истёк по заказу\n"
        "https://playerok.com/deal/{deal_id}\n\n"
        "В ближайшие несколько секунд, ваша сессия будет закрыта\n"
        "Пожалуйста, не забудьте подтвердить заказ и оставить отзыв\n\n"
        "💡 Если нужна помощь, позовите продавца командой !продавец, и он вам поможет"
    ),
    "reminder": (
        "⏰ Внимание!\n"
        "До окончания аренды осталось {time_left}.\n"
        "Сохраните прогресс — после окончания сессия будет закрыта."
    ),
    "guard_no_rental": (
        "ℹ️ Команда `!steamguard` работает только после оплаты аренды."
    ),
    "guard_wrong_alias": (
        "ℹ️ У вас активная аренда аккаунта `{actual}`. "
        "Команда должна быть: `!steamguard {actual}`."
    ),
    "no_accounts": (
        "❌ Извините, сейчас нет свободных аккаунтов для аренды. "
        "Продавцу отправлено уведомление, он вернётся к вам в ближайшее время."
    ),
}


DEFAULT_CONFIG: dict[str, Any] = {
    # Соответствие конкретного Playerok item_id → параметрам аренды.
    # {"itemId": {"duration_minutes": 120, "aliases": ["acc1"]}}
    "lot_map": {},
    # Если duration не определена через lot_map — парсить из названия товара.
    "auto_parse_duration": True,
    # За сколько минут до конца аренды слать напоминание.
    "reminder_minutes_before": 15,
    # Брать любой свободный аккаунт, если по lot_map не нашли подходящий.
    "fallback_any_account": True,
    # После истечения аренды — пытаться сменить пароль и/или отозвать сессии
    # (через steampy). Сейчас по умолчанию ВЫКЛ — это требует mobile-confirm
    # и стабильного логина; включай осознанно.
    "auto_revoke_sessions": False,
    "auto_change_password": False,
    # Pre-reservation: на NEW_DEAL аккаунт блокируется на N минут, чтобы
    # параллельная вторая покупка не получила тот же аккаунт. После
    # ITEM_PAID резерв превращается в выдачу. Если в течение N минут
    # оплата так и не пришла — резерв сбрасывается.
    "pre_reservation_enabled": True,
    "pre_reservation_minutes": 5,
    # Бан-лист покупателей (по username/id/email). Покупатели из этого
    # списка игнорируются на NEW_DEAL/ITEM_PAID.
    "buyer_blacklist": [],
    # Авто-добавление в blacklist при `DEAL_HAS_PROBLEM` / refund.
    "auto_blacklist_on_refund": True,
    # Warmup: периодически логинимся в неиспользуемый аккаунт, чтобы Steam
    # не помечал его как dormant. login + idle N сек + выход.
    "warmup_enabled": False,
    "warmup_interval_days": 7,
    "warmup_idle_seconds": 30,
    "warmup_check_interval_hours": 6,
    "templates": dict(DEFAULT_TEMPLATES),
    "alert_no_accounts": True,
}


# ─── Конфиг ──────────────────────────────────────────────────────

def get_config() -> dict[str, Any]:
    cfg = common.load_json(CONFIG_FILE, None)
    if cfg is None:
        cfg = dict(DEFAULT_CONFIG)
        cfg["templates"] = dict(DEFAULT_TEMPLATES)
    else:
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v if not isinstance(v, dict) else dict(v)
        if not isinstance(cfg.get("templates"), dict):
            cfg["templates"] = dict(DEFAULT_TEMPLATES)
        else:
            for tk, tv in DEFAULT_TEMPLATES.items():
                cfg["templates"].setdefault(tk, tv)
    common.save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    common.save_json(CONFIG_FILE, cfg)


def render_template(name: str, **kwargs: Any) -> str:
    cfg = get_config()
    tpl = cfg.get("templates", {}).get(name) or DEFAULT_TEMPLATES.get(name, "")
    return common.fmt_template(tpl, **kwargs)


# ─── Хранилище аккаунтов ─────────────────────────────────────────

def list_accounts() -> list[dict[str, Any]]:
    return common.load_json(ACCOUNTS_FILE, [])


def save_accounts(accs: list[dict[str, Any]]) -> None:
    common.save_json(ACCOUNTS_FILE, accs)


def find_account(alias: str) -> dict[str, Any] | None:
    al = (alias or "").lower()
    for a in list_accounts():
        if a.get("alias", "").lower() == al:
            return a
    return None


def upsert_account(acc: dict[str, Any]) -> None:
    accs = list_accounts()
    al = acc.get("alias", "").lower()
    for i, a in enumerate(accs):
        if a.get("alias", "").lower() == al:
            accs[i] = acc
            save_accounts(accs)
            return
    accs.append(acc)
    save_accounts(accs)


def delete_account(alias: str) -> bool:
    al = (alias or "").lower()
    accs = list_accounts()
    new_accs = [a for a in accs if a.get("alias", "").lower() != al]
    if len(new_accs) == len(accs):
        return False
    save_accounts(new_accs)
    return True


def _account_in_use(alias: str) -> bool:
    """True если аккаунт сейчас занят (выдан, активен или зарезервирован)."""
    al = alias.lower()
    now = common.now()
    for a in list_assignments():
        if a.get("alias", "").lower() != al:
            continue
        status = a.get("status")
        if status in ("active", "delivered"):
            return True
        if status == "reserved" and a.get("reserved_until", 0) > now:
            return True
    return False


def pick_free_account(preferred_aliases: list[str] | None = None,
                      fallback_any: bool = True) -> dict[str, Any] | None:
    accs = list_accounts()
    pool = accs
    if preferred_aliases:
        prefs = {a.lower() for a in preferred_aliases}
        pool = [a for a in accs if a.get("alias", "").lower() in prefs]
        if not pool and fallback_any:
            pool = accs
    for a in pool:
        if a.get("frozen"):
            continue
        if _account_in_use(a.get("alias", "")):
            continue
        return a
    return None


# ─── Хранилище assignments ───────────────────────────────────────

def list_assignments() -> list[dict[str, Any]]:
    return common.load_json(ASSIGNMENTS_FILE, [])


def save_assignments(items: list[dict[str, Any]]) -> None:
    common.save_json(ASSIGNMENTS_FILE, items)


def find_assignment_by_chat(chat_id: str) -> dict[str, Any] | None:
    chat_id = str(chat_id)
    items = [a for a in list_assignments()
             if str(a.get("chat_id")) == chat_id
             and a.get("status") in ("delivered", "active")]
    if not items:
        return None
    items.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    return items[0]


def find_assignment_by_deal(deal_id: str) -> dict[str, Any] | None:
    for a in list_assignments():
        if a.get("deal_id") == deal_id:
            return a
    return None


def add_assignment(item: dict[str, Any]) -> None:
    items = list_assignments()
    items.append(item)
    if len(items) > 5000:
        items = items[-5000:]
    save_assignments(items)


def update_assignment(deal_id: str, **fields: Any) -> dict[str, Any] | None:
    items = list_assignments()
    for a in items:
        if a.get("deal_id") == deal_id:
            a.update(fields)
            save_assignments(items)
            return a
    return None


def log_event(event: str, **extra: Any) -> None:
    entry = {"ts": common.now(), "event": event}
    entry.update({k: v for k, v in extra.items() if v is not None})
    history = common.load_json(HISTORY_FILE, [])
    history.append(entry)
    if len(history) > 10000:
        history = history[-10000:]
    common.save_json(HISTORY_FILE, history)


# ─── Blacklist покупателей ───────────────────────────────────────

def _buyer_keys(deal: Any) -> list[str]:
    """Извлекает все возможные «идентификаторы» покупателя для проверки
    blacklist — username, id, email — все в lowercase."""
    user = getattr(deal, "user", None)
    if user is None:
        return []
    out: list[str] = []
    for attr in ("username", "id", "email"):
        v = getattr(user, attr, None)
        if v:
            out.append(str(v).strip().lower())
    return out


def is_buyer_blacklisted(deal: Any) -> str | None:
    """Возвращает совпавшее значение из blacklist или None."""
    bl = [str(x).strip().lower() for x in (get_config().get("buyer_blacklist") or [])]
    if not bl:
        return None
    keys = _buyer_keys(deal)
    for k in keys:
        if k in bl:
            return k
    return None


def add_buyer_to_blacklist(value: str) -> bool:
    cfg = get_config()
    bl = list(cfg.get("buyer_blacklist") or [])
    v = (value or "").strip().lower()
    if not v or v in [str(x).lower() for x in bl]:
        return False
    bl.append(v)
    cfg["buyer_blacklist"] = bl
    save_config(cfg)
    return True


def remove_buyer_from_blacklist(value: str) -> bool:
    cfg = get_config()
    v = (value or "").strip().lower()
    bl = [x for x in (cfg.get("buyer_blacklist") or [])
          if str(x).strip().lower() != v]
    if len(bl) == len(cfg.get("buyer_blacklist") or []):
        return False
    cfg["buyer_blacklist"] = bl
    save_config(cfg)
    return True


# ─── Pre-reservation утилиты ─────────────────────────────────────

def find_reservation_by_deal(deal_id: str) -> dict[str, Any] | None:
    for a in list_assignments():
        if a.get("deal_id") == deal_id and a.get("status") == "reserved":
            return a
    return None


def _find_by_short(short: str) -> dict[str, Any] | None:
    for a in list_assignments():
        if common.short_id(a.get("deal_id", "")) == short:
            return a
    return None


def _operator_extend(short: str, add_min: int,
                     ctx: PluginContext) -> dict[str, Any] | None:
    """Продляет активную аренду на add_min минут. Обновляет expires_at,
    шлёт уведомление покупателю."""
    a = _find_by_short(short)
    if a is None or a.get("status") not in ("active", "delivered"):
        return None
    cur_exp = int(a.get("expires_at", 0) or 0)
    new_exp = (cur_exp if cur_exp else common.now()) + add_min * 60
    a = update_assignment(a["deal_id"],
                          expires_at=new_exp,
                          duration_minutes=(
                              int(a.get("duration_minutes", 0)) + add_min),
                          reminder_sent=False) or a
    log_event("operator_extend", deal_id=a.get("deal_id"),
              alias=a.get("alias"), add_minutes=add_min)
    chat_id = a.get("chat_id")
    if chat_id:
        _safe_send(
            ctx, str(chat_id),
            f"➕ Время аренды увеличено на {add_min} мин.\n"
            f"Новое окончание: {common.fmt_ts(new_exp)}")
    return a


def _operator_stop(short: str, ctx: PluginContext) -> dict[str, Any] | None:
    """Помечает аренду как expired, шлёт сообщение покупателю и
    спавнит revoke/change_password (если включено)."""
    a = _find_by_short(short)
    if a is None or a.get("status") not in ("active", "delivered"):
        return None
    now = common.now()
    a = update_assignment(a["deal_id"], status="expired",
                          expires_at=now, expired_at=now,
                          stopped_by_operator=True) or a
    log_event("operator_stop", deal_id=a.get("deal_id"),
              alias=a.get("alias"))
    chat_id = a.get("chat_id")
    if chat_id:
        _safe_send(ctx, str(chat_id), render_template(
            "expired", deal_id=a.get("deal_id")))
    cfg = get_config()
    if cfg.get("auto_revoke_sessions") or cfg.get("auto_change_password"):
        alias = a.get("alias", "")
        acc = find_account(alias)
        if acc:
            threading.Thread(
                target=HANDLER._post_expire_actions,
                args=(ctx, dict(acc),
                      bool(cfg.get("auto_revoke_sessions")),
                      bool(cfg.get("auto_change_password"))),
                daemon=True,
                name=f"asr-stop-{alias}",
            ).start()
    return a


def _operator_switch_account(short: str,
                             ctx: PluginContext) -> dict[str, Any] | str | None:
    """Меняет аккаунт активной аренды на свободный из пула. Старый помечает
    expired, новый — delivered (таймер сбрасывается под старое expires_at)."""
    a = _find_by_short(short)
    if a is None or a.get("status") not in ("active", "delivered"):
        return None
    old_alias = a.get("alias", "")
    old_acc = find_account(old_alias)
    new_acc = pick_free_account()
    if new_acc is None:
        return "❌ Нет свободных аккаунтов для замены."
    if new_acc.get("alias") == old_alias:
        return "❌ Единственный свободный — текущий. Замена не нужна."

    now = common.now()
    duration_min = int(a.get("duration_minutes", 0))
    # Закрываем старую аренду
    update_assignment(a["deal_id"], status="expired",
                      expired_at=now, replaced_by=new_acc.get("alias"))
    log_event("operator_switch_close", deal_id=a.get("deal_id"),
              alias=old_alias, new_alias=new_acc.get("alias"))
    if old_acc:
        cfg = get_config()
        if cfg.get("auto_revoke_sessions") or cfg.get("auto_change_password"):
            threading.Thread(
                target=HANDLER._post_expire_actions,
                args=(ctx, dict(old_acc),
                      bool(cfg.get("auto_revoke_sessions")),
                      bool(cfg.get("auto_change_password"))),
                daemon=True,
                name=f"asr-switch-{old_alias}",
            ).start()

    # Создаём новую запись на тот же deal_id с suffix
    new_deal_id = f"{a['deal_id']}/switch-{now}"
    add_assignment({
        "deal_id": new_deal_id,
        "alias": new_acc.get("alias", ""),
        "buyer": a.get("buyer", "?"),
        "chat_id": a.get("chat_id"),
        "item_id": a.get("item_id", ""),
        "item_name": a.get("item_name", ""),
        "duration_minutes": duration_min,
        "created_at": now,
        "started_at": 0,
        "expires_at": 0,
        "reminder_sent": False,
        "status": "delivered",
        "switched_from": a["deal_id"],
    })
    chat_id = a.get("chat_id")
    if chat_id:
        text = render_template(
            "issue",
            login=new_acc.get("login", ""),
            password=new_acc.get("password", ""),
            duration=common.human_minutes(duration_min),
            alias=new_acc.get("alias", ""),
            game=a.get("item_name", "Steam"),
        )
        _safe_send(ctx, str(chat_id),
                   "🔁 Продавец заменил аккаунт. Новые данные ниже.\n\n" + text)
    bump_stat(new_acc.get("alias", ""), delivered_count=1)
    return new_acc


def release_expired_reservations() -> int:
    """Сбрасывает резервации, по которым оплата не пришла за N минут.
    Возвращает число освобождённых."""
    items = list_assignments()
    now = common.now()
    changed = 0
    for a in items:
        if a.get("status") == "reserved" and a.get("reserved_until", 0) <= now:
            a["status"] = "reservation_expired"
            a["released_at"] = now
            changed += 1
    if changed:
        save_assignments(items)
    return changed


def _stats_account(alias: str) -> dict[str, Any]:
    """Достаёт блок per-account статистики (создаёт, если нужно)."""
    acc = find_account(alias)
    if not acc:
        return {}
    stats = acc.get("stats")
    if not isinstance(stats, dict):
        stats = {
            "rentals_count": 0,        # успешно завершённых аренд
            "active_count": 0,         # сколько раз попадал в активную аренду
            "delivered_count": 0,      # сколько раз выдавался
            "total_minutes": 0,        # суммарно сданных минут
            "total_revenue": 0.0,      # рублей по всем сделкам
            "first_used_at": 0,
            "last_used_at": 0,
        }
        acc["stats"] = stats
        upsert_account(acc)
    return stats


def bump_stat(alias: str, **delta: Any) -> None:
    """Инкрементирует поля per-account статистики и обновляет timestamp."""
    if not alias:
        return
    acc = find_account(alias)
    if not acc:
        return
    stats = acc.get("stats") or {}
    for k, v in delta.items():
        cur = stats.get(k, 0)
        if isinstance(v, (int, float)) and isinstance(cur, (int, float)):
            stats[k] = cur + v
        else:
            stats[k] = v
    ts = common.now()
    if not stats.get("first_used_at"):
        stats["first_used_at"] = ts
    stats["last_used_at"] = ts
    acc["stats"] = stats
    upsert_account(acc)


# ─── Метаданные плагина ──────────────────────────────────────────

PLUGIN = Plugin(
    id="autosteamrental",
    name="autosteamrental",
    icon="🎮",
    description=(
        "Модуль, автоматизирующий аренду Steam аккаунтов. "
        "/autosteamrental в Telegram-боте для управления."
    ),
    instruction=(
        "*🎮 autosteamrental*\n\n"
        "*Что делает плагин:*\n"
        "• После оплаты аренды бот сам отправляет логин/пароль и длительность "
        "аренды в чат Playerok.\n"
        "• Команда `!steamguard <ник>` в чате Playerok отдаёт Steam Guard "
        "код и стартует таймер аренды (с момента первого получения кода).\n"
        "• За 15 минут до конца аренды бот шлёт напоминание, по истечении — "
        "сообщает об окончании и закрывает выдачу.\n\n"
        "*Как настроить:*\n"
        "1. Включи плагин кнопкой ниже.\n"
        "2. `/autosteamrental` → «Аккаунты» → «Добавить» → пришли `.maFile` "
        "(или ZIP со всеми) + логин/пароль.\n"
        "3. В «Настройках» можно настроить шаблоны и сопоставление лотов "
        "с длительностью.\n\n"
        "*Команды покупателю:*\n"
        "• `!steamguard <ник>` — получить Steam Guard код.\n"
        "• `!продавец` — позвать продавца."
    ),
    default_enabled=True,
    keywords=("!steamguard", "аренда", "rental", "steam"),
)


# ─── Утилиты отправки ────────────────────────────────────────────

def _safe_send(ctx: PluginContext, chat_id: str, text: str) -> bool:
    if not ctx.playerok_acc:
        ctx.log.warning("autosteamrental: playerok_acc is None, не отправляю в чат %s", chat_id)
        return False
    try:
        ctx.playerok_acc.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:
        ctx.log.error("autosteamrental: send_message failed: %s", exc)
        return False


def _notify_admin(ctx: PluginContext, text: str, parse_mode: str | None = "Markdown") -> None:
    try:
        ctx.bot.send_message(ctx.admin_id, text, parse_mode=parse_mode)
    except Exception:
        try:
            ctx.bot.send_message(ctx.admin_id, text)
        except Exception:
            ctx.log.debug("autosteamrental: admin notify failed", exc_info=True)


def _looks_rental_item(item_name: str, comment: str | None = None) -> bool:
    if not item_name:
        return False
    s = (item_name + " " + (comment or "")).lower()
    return any(kw in s for kw in (
        "аренда", "арендовать", "прокат", "rental", "rent", " на ",
    ))


def _resolve_duration(item: Any, lot: dict[str, Any] | None,
                      cfg: dict[str, Any]) -> int | None:
    if lot and lot.get("duration_minutes"):
        return int(lot["duration_minutes"])
    if not cfg.get("auto_parse_duration", True):
        return None
    if item is None:
        return None
    name = getattr(item, "name", "") or ""
    desc = getattr(item, "description", "") or ""
    return common.parse_duration_minutes(name + " " + desc)


# ─── Главный обработчик ──────────────────────────────────────────

class Handler:
    """Логика плагина."""

    _bg_thread: threading.Thread | None = None
    _bg_stop = False
    _last_warmup_check: int = 0
    _warmup_running: bool = False

    # --- события Playerok -------------------------------------------------

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        if etype is EventTypes.ITEM_PAID:
            return self._handle_item_paid(event, ctx)
        if etype is EventTypes.NEW_DEAL:
            return self._handle_new_deal(event, ctx)
        if etype is EventTypes.NEW_MESSAGE:
            return self._handle_new_message(event, ctx)
        # Refund / откат / спор — авто-blacklist если включено.
        problem_type = getattr(EventTypes, "DEAL_HAS_PROBLEM", None)
        if problem_type is not None and etype is problem_type:
            return self._handle_deal_problem(event, ctx)
        return False

    def _handle_new_deal(self, event: Any, ctx: PluginContext) -> bool:
        """Pre-reservation: на NEW_DEAL резервируем аккаунт до ITEM_PAID
        (или истечения 5 мин)."""
        cfg = get_config()
        if not cfg.get("pre_reservation_enabled", True):
            # Без резервации просто дублируем логику выдачи (как раньше).
            return self._handle_item_paid(event, ctx)

        deal = getattr(event, "deal", None)
        if deal is None:
            return False
        deal_id = getattr(deal, "id", None)
        if not deal_id:
            return False

        # Blacklist
        bl_match = is_buyer_blacklisted(deal)
        if bl_match:
            log_event("blocked_by_blacklist", deal_id=deal_id, match=bl_match)
            _notify_admin(
                ctx,
                f"🚫 *autosteamrental*: NEW_DEAL заблокирован blacklist'ом\n"
                f"совпадение: `{common.md_escape(bl_match)}`\n"
                f"🆔 `{deal_id}`",
            )
            return True

        # Уже есть запись (резерв или выдача) — игнорим.
        if find_assignment_by_deal(deal_id):
            return True

        item = getattr(deal, "item", None)
        item_name = getattr(item, "name", "") if item else ""
        item_id = getattr(item, "id", "") if item else ""
        chat = getattr(event, "chat", None) or getattr(deal, "chat", None)
        chat_id = getattr(chat, "id", None) if chat else None
        if not chat_id:
            return False

        lot = cfg.get("lot_map", {}).get(item_id) if item_id else None
        if lot is None:
            comment = (getattr(item, "description", None)
                       or getattr(deal, "comment_from_buyer", None))
            if not _looks_rental_item(item_name, comment):
                return False

        duration_min = _resolve_duration(item, lot, cfg)
        if duration_min is None:
            # На NEW_DEAL ничего не делаем — пусть ITEM_PAID разбирается.
            return False

        preferred = (lot or {}).get("aliases") or None
        acc = pick_free_account(
            preferred, fallback_any=cfg.get("fallback_any_account", True))
        if acc is None:
            # Не резервируем — на ITEM_PAID попробуем ещё раз.
            return False

        buyer = getattr(getattr(deal, "user", None), "username", None) or "?"
        reserve_min = int(cfg.get("pre_reservation_minutes", 5))
        now = common.now()
        add_assignment({
            "deal_id": deal_id,
            "alias": acc.get("alias", ""),
            "buyer": buyer,
            "chat_id": str(chat_id),
            "item_id": item_id,
            "item_name": item_name,
            "duration_minutes": duration_min,
            "created_at": now,
            "reserved_until": now + reserve_min * 60,
            "started_at": 0,
            "expires_at": 0,
            "reminder_sent": False,
            "status": "reserved",
        })
        log_event("reserved", deal_id=deal_id, alias=acc.get("alias"),
                  buyer=buyer, reserve_minutes=reserve_min)
        ctx.log.info("autosteamrental: reserved %s for deal %s (%d min)",
                     acc.get("alias"), deal_id, reserve_min)
        return True

    def _handle_deal_problem(self, event: Any, ctx: PluginContext) -> bool:
        """Откат / спор: если включено — заносим покупателя в blacklist."""
        cfg = get_config()
        if not cfg.get("auto_blacklist_on_refund", True):
            return False
        deal = getattr(event, "deal", None)
        if deal is None:
            return False
        deal_id = getattr(deal, "id", "?")
        keys = _buyer_keys(deal)
        added = []
        for k in keys:
            if add_buyer_to_blacklist(k):
                added.append(k)
        if added:
            log_event("refund_auto_blacklist", deal_id=deal_id, keys=added)
            _notify_admin(
                ctx,
                f"🚫 *autosteamrental*: авто-blacklist по DEAL\\_HAS\\_PROBLEM\n"
                f"🆔 `{deal_id}`\n"
                f"добавлено: {', '.join(f'`{common.md_escape(k)}`' for k in added)}",
            )
        return True

    def _handle_item_paid(self, event: Any, ctx: PluginContext) -> bool:
        deal = getattr(event, "deal", None)
        if deal is None:
            return False
        deal_id = getattr(deal, "id", None)
        if not deal_id:
            return False

        # Blacklist
        bl_match = is_buyer_blacklisted(deal)
        if bl_match:
            log_event("blocked_by_blacklist_paid",
                      deal_id=deal_id, match=bl_match)
            _notify_admin(
                ctx,
                f"🚫 *autosteamrental*: ITEM_PAID от blacklist-покупателя\n"
                f"совпадение: `{common.md_escape(bl_match)}`\n"
                f"🆔 `{deal_id}` — выдача пропущена",
            )
            return True

        # Если есть pre-reservation на этот же deal_id — превращаем её в выдачу.
        reservation = find_reservation_by_deal(deal_id)
        if reservation is not None:
            return self._promote_reservation(reservation, event, ctx)

        # Если выдача уже была (повтор ITEM_PAID) — игнор.
        existing = find_assignment_by_deal(deal_id)
        if existing is not None:
            return True

        item = getattr(deal, "item", None)
        item_name = getattr(item, "name", "") if item else ""
        item_id = getattr(item, "id", "") if item else ""
        chat = getattr(event, "chat", None) or getattr(deal, "chat", None)
        chat_id = getattr(chat, "id", None) if chat else None
        if not chat_id:
            return False

        cfg = get_config()
        lot = cfg.get("lot_map", {}).get(item_id) if item_id else None
        if lot is None:
            # Авто-режим: проверяем, что это похоже на аренду.
            comment = getattr(item, "description", None) or getattr(deal, "comment_from_buyer", None)
            if not _looks_rental_item(item_name, comment):
                return False

        duration_min = _resolve_duration(item, lot, cfg)
        if duration_min is None:
            # Если не смогли определить длительность — не подхватываем.
            _notify_admin(
                ctx,
                f"⚠️ *autosteamrental*: не удалось определить длительность аренды\n"
                f"🛒 {common.md_escape(item_name)}\n🆔 `{deal_id}`\n\n"
                "Задай duration\\_minutes в lot\\_map плагина.",
            )
            log_event("no_duration", deal_id=deal_id, item=item_name)
            return False

        preferred = (lot or {}).get("aliases") or None
        acc = pick_free_account(preferred, fallback_any=cfg.get("fallback_any_account", True))
        if acc is None:
            _safe_send(ctx, chat_id, render_template("no_accounts"))
            if cfg.get("alert_no_accounts", True):
                _notify_admin(
                    ctx,
                    f"⚠️ *autosteamrental*: нет свободных аккаунтов\n"
                    f"🛒 {common.md_escape(item_name)}\n🆔 `{deal_id}`",
                )
            log_event("no_accounts", deal_id=deal_id, item=item_name)
            return True

        buyer = getattr(getattr(deal, "user", None), "username", None) or "?"
        duration_human = common.human_minutes(duration_min)

        text = render_template(
            "issue",
            login=acc.get("login", ""),
            password=acc.get("password", ""),
            duration=duration_human,
            alias=acc.get("alias", ""),
            game=item_name or "Steam",
        )
        if not _safe_send(ctx, chat_id, text):
            _notify_admin(
                ctx,
                f"❌ *autosteamrental*: не удалось отправить выдачу\n🆔 `{deal_id}`",
            )
            return True

        add_assignment({
            "deal_id": deal_id,
            "alias": acc.get("alias", ""),
            "buyer": buyer,
            "chat_id": str(chat_id),
            "item_id": item_id,
            "item_name": item_name,
            "duration_minutes": duration_min,
            "created_at": common.now(),
            "started_at": 0,  # появится при первом !steamguard
            "expires_at": 0,
            "reminder_sent": False,
            "status": "delivered",  # выдан, таймер ещё не запущен
        })
        log_event("delivered", deal_id=deal_id, alias=acc.get("alias"),
                  buyer=buyer, duration_minutes=duration_min)
        bump_stat(acc.get("alias", ""), delivered_count=1)
        _notify_admin(
            ctx,
            f"🎮 *autosteamrental*: выдан аккаунт\n"
            f"🛒 {common.md_escape(item_name)}\n"
            f"👤 {common.md_escape(buyer)}\n"
            f"🔑 `{acc.get('alias', '')}`\n"
            f"⏱ Длительность: {duration_human}\n"
            f"🆔 `{deal_id}`",
        )
        return True

    def _promote_reservation(self, reservation: dict[str, Any],
                             event: Any, ctx: PluginContext) -> bool:
        """ITEM_PAID для уже зарезервированного deal — отправляем выдачу."""
        deal = getattr(event, "deal", None)
        chat = (getattr(event, "chat", None)
                or getattr(deal, "chat", None) if deal else None)
        chat_id = (getattr(chat, "id", None)
                   if chat else reservation.get("chat_id"))
        if not chat_id:
            return False
        alias = reservation.get("alias", "")
        acc = find_account(alias)
        if acc is None:
            # Аккаунт удалён за время резерва — fallback на обычную выдачу.
            update_assignment(reservation["deal_id"],
                              status="reservation_account_missing")
            return self._handle_item_paid(event, ctx)

        duration_min = int(reservation.get("duration_minutes", 0))
        duration_human = common.human_minutes(duration_min)
        item_name = reservation.get("item_name", "")
        text = render_template(
            "issue",
            login=acc.get("login", ""),
            password=acc.get("password", ""),
            duration=duration_human,
            alias=alias,
            game=item_name or "Steam",
        )
        if not _safe_send(ctx, chat_id, text):
            _notify_admin(
                ctx,
                f"❌ *autosteamrental*: не удалось отправить выдачу (promote)\n"
                f"🆔 `{reservation['deal_id']}`",
            )
            return True

        update_assignment(
            reservation["deal_id"],
            status="delivered",
            delivered_at=common.now(),
        )
        log_event("delivered", deal_id=reservation["deal_id"], alias=alias,
                  buyer=reservation.get("buyer"), duration_minutes=duration_min,
                  from_reservation=True)
        bump_stat(alias, delivered_count=1)
        _notify_admin(
            ctx,
            f"🎮 *autosteamrental*: выдан аккаунт (по резерву)\n"
            f"🛒 {common.md_escape(item_name)}\n"
            f"👤 {common.md_escape(reservation.get('buyer', '?'))}\n"
            f"🔑 `{alias}`\n"
            f"⏱ Длительность: {duration_human}\n"
            f"🆔 `{reservation['deal_id']}`",
        )
        return True

    def _handle_new_message(self, event: Any, ctx: PluginContext) -> bool:
        msg = getattr(event, "message", None)
        if msg is None:
            return False
        user = getattr(msg, "user", None)
        if user is None or not ctx.playerok_acc:
            return False
        if getattr(user, "id", None) == getattr(ctx.playerok_acc, "id", None):
            return False
        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            return False
        low = text.lower()
        chat = getattr(event, "chat", None)
        chat_id = getattr(chat, "id", None) if chat else None
        if not chat_id:
            return False
        if not low.startswith("!steamguard"):
            return False

        # Аргумент команды — алиас (ник аккаунта).
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        assignment = find_assignment_by_chat(str(chat_id))
        if assignment is None:
            _safe_send(ctx, str(chat_id), render_template("guard_no_rental"))
            return True

        actual_alias = assignment.get("alias", "")
        if arg and arg.lower() != actual_alias.lower():
            _safe_send(ctx, str(chat_id),
                       render_template("guard_wrong_alias", actual=actual_alias))
            return True

        acc = find_account(actual_alias)
        if acc is None:
            _safe_send(ctx, str(chat_id), "❌ Внутренняя ошибка: аккаунт удалён.")
            return True

        code = common.generate_code(acc.get("mafile", {}).get("shared_secret", ""))
        if not code:
            _safe_send(ctx, str(chat_id), "❌ Не удалось сгенерировать код. Обратитесь к продавцу.")
            return True

        # Если таймер не стартовал — стартуем сейчас.
        if not assignment.get("started_at"):
            started = common.now()
            duration_min = int(assignment.get("duration_minutes", 0) or 0)
            ends = started + duration_min * 60
            update_assignment(
                assignment["deal_id"],
                started_at=started,
                expires_at=ends,
                status="active",
            )
            body = render_template(
                "guard_code_first",
                code=code,
                started_at=common.fmt_ts(started),
                ends_at=common.fmt_ts(ends),
                duration=common.human_minutes(duration_min),
            )
            log_event("rental_started", deal_id=assignment["deal_id"],
                      alias=actual_alias, started_at=started, ends_at=ends)
            bump_stat(actual_alias, active_count=1)
        else:
            ends = int(assignment.get("expires_at", 0) or 0)
            left = max(0, ends - common.now())
            body = render_template(
                "guard_code",
                code=code,
                time_left=common.human_seconds(left),
            )
            log_event("guard_issued",
                      deal_id=assignment.get("deal_id"),
                      alias=actual_alias)

        _safe_send(ctx, str(chat_id), body)
        return True

    # --- фоновый чекер истечения / напоминаний ----------------------------

    def start_background(self, ctx: PluginContext) -> None:
        if self._bg_thread and self._bg_thread.is_alive():
            return
        # Recovery: при старте проверяем подвисшие резервации и активные
        # аренды (например, после краха VM — кронах сразу подхватываем
        # просроченные таймеры).
        try:
            self._recover_on_start(ctx)
        except Exception:
            LOGGER.exception("autosteamrental: recover_on_start failed")

        self._bg_stop = False
        self._bg_thread = threading.Thread(
            target=self._bg_loop, args=(ctx,), daemon=True,
            name="autosteamrental-checker",
        )
        self._bg_thread.start()
        LOGGER.info("autosteamrental: чекер истечения аренды запущен")

    def _recover_on_start(self, ctx: PluginContext) -> None:
        """После рестарта: чистим просроченные резервации и помечаем
        просроченные active как expired (без mafile-операций, чтобы
        Steam-API не дёрнуть из-под крашед-VM, но видимое состояние
        корректное)."""
        now = common.now()
        items = list_assignments()
        changed = False
        recovered_reservations = 0
        recovered_expired = 0
        for a in items:
            status = a.get("status")
            if status == "reserved" and a.get("reserved_until", 0) <= now:
                a["status"] = "reservation_expired"
                a["released_at"] = now
                recovered_reservations += 1
                changed = True
            elif status == "active":
                exp = int(a.get("expires_at", 0) or 0)
                if exp and exp <= now:
                    a["status"] = "expired"
                    a["expired_at"] = now
                    a["recovered"] = True
                    recovered_expired += 1
                    changed = True
        if changed:
            save_assignments(items)
            LOGGER.info(
                "autosteamrental: recovery — резерваций сброшено=%d, "
                "истёкших аренд помечено=%d",
                recovered_reservations, recovered_expired)
            if recovered_expired or recovered_reservations:
                _notify_admin(
                    ctx,
                    f"♻️ *autosteamrental*: recovery после рестарта\n"
                    f"• Сброшено резерваций: {recovered_reservations}\n"
                    f"• Помечено истёкшими: {recovered_expired}",
                )

    def _bg_loop(self, ctx: PluginContext) -> None:
        while not self._bg_stop:
            try:
                self._tick(ctx)
            except Exception:
                LOGGER.exception("autosteamrental: ошибка в фоновом чекере")
            # Шаг 15 секунд — достаточно для коротких аренд (от 15 секунд).
            for _ in range(15):
                if self._bg_stop:
                    return
                time.sleep(1)

    def _tick(self, ctx: PluginContext) -> None:
        cfg = get_config()
        reminder_min = int(cfg.get("reminder_minutes_before", 15))
        now_ts = common.now()
        items = list_assignments()
        changed = False
        for a in items:
            # Истёкшие резервации
            if a.get("status") == "reserved":
                if a.get("reserved_until", 0) <= now_ts:
                    a["status"] = "reservation_expired"
                    a["released_at"] = now_ts
                    changed = True
                    log_event("reservation_expired", deal_id=a.get("deal_id"),
                              alias=a.get("alias"))
                continue
            if a.get("status") != "active":
                continue
            ends = int(a.get("expires_at", 0) or 0)
            if not ends:
                continue
            chat_id = str(a.get("chat_id"))
            # Напоминание
            if reminder_min > 0 and not a.get("reminder_sent"):
                if ends - now_ts <= reminder_min * 60 and ends - now_ts > 0:
                    _safe_send(ctx, chat_id, render_template(
                        "reminder",
                        time_left=common.human_seconds(ends - now_ts),
                    ))
                    a["reminder_sent"] = True
                    changed = True
                    log_event("reminder_sent", deal_id=a.get("deal_id"),
                              alias=a.get("alias"))
            # Истечение
            if now_ts >= ends:
                _safe_send(ctx, chat_id, render_template(
                    "expired", deal_id=a.get("deal_id"),
                ))
                a["status"] = "expired"
                a["expired_at"] = now_ts
                changed = True
                log_event("rental_expired", deal_id=a.get("deal_id"),
                          alias=a.get("alias"))
                bump_stat(
                    a.get("alias", ""),
                    rentals_count=1,
                    total_minutes=int(a.get("duration_minutes", 0)),
                )
                # Дополнительные действия: revoke sessions / смена пароля.
                # Поднимаем Steam-сессию и фигачим их в отдельном потоке —
                # это медленные HTTP-запросы и mobile-confirm через .maFile.
                if (cfg.get("auto_revoke_sessions")
                        or cfg.get("auto_change_password")):
                    alias = a.get("alias", "")
                    acc = find_account(alias)
                    if acc:
                        threading.Thread(
                            target=self._post_expire_actions,
                            args=(ctx, dict(acc), bool(cfg.get("auto_revoke_sessions")),
                                  bool(cfg.get("auto_change_password"))),
                            daemon=True,
                            name=f"asr-post-expire-{alias}",
                        ).start()
        if changed:
            save_assignments(items)

        # Warmup-цикл — отдельный путь, без блокировки.
        try:
            self._warmup_tick(ctx)
        except Exception:
            LOGGER.exception("autosteamrental: warmup_tick failed")

    # --- post-expire (revoke sessions / change password) ------------------

    def _post_expire_actions(self, ctx: PluginContext, acc: dict[str, Any],
                             do_revoke: bool, do_change_pw: bool) -> None:
        """Поднимает Steam-сессию и делает revoke/change_password.

        Вынесено в отдельный поток — это медленные HTTP-запросы и
        mobile-confirm через .maFile. На любую ошибку — пишем в логи и
        в Telegram админу, аккаунт оставляем как есть (либо помечаем
        `frozen`, если ошибок подряд накопилось много).
        """
        alias = acc.get("alias", "")
        if steam_session is None:
            _notify_admin(
                ctx,
                f"⚠️ *autosteamrental*: не могу поднять Steam-сессию для "
                f"`{alias}` — модуль `_steam_session` не загрузился. "
                f"Проверь зависимости (`rsa`, `steampy`).",
            )
            return

        mafile = acc.get("mafile") or {}
        shared = mafile.get("shared_secret") or ""
        identity = mafile.get("identity_secret") or ""
        steamid = ""
        sess_block = mafile.get("Session") or {}
        if isinstance(sess_block, dict):
            steamid = str(sess_block.get("SteamID") or "")
        steamid = steamid or str(mafile.get("steamid") or "")
        login = acc.get("login") or mafile.get("account_name") or alias
        password = acc.get("password") or ""

        if not all([shared, identity, login, password]):
            _notify_admin(
                ctx,
                f"⚠️ *autosteamrental*: для `{alias}` неполные данные "
                f"(нет shared_secret / identity_secret / логина / пароля). "
                f"Skip revoke + change_password.",
            )
            return

        try:
            sess = steam_session.SteamSession(
                account_name=login, password=password,
                shared_secret=shared, identity_secret=identity,
                steamid=steamid or None,
            )
            sess.login()
        except Exception as exc:
            LOGGER.error("login Steam для %s упал", alias, exc_info=True)
            _notify_admin(
                ctx,
                f"❌ *autosteamrental*: login Steam для `{alias}` упал — `{exc}`. "
                f"revoke/change_password пропущены.",
            )
            self._track_account_failure(alias)
            return

        notes: list[str] = []

        if do_revoke:
            try:
                ok = sess.revoke_all_other_sessions()
                if ok:
                    notes.append("✅ сессии отозваны")
                    log_event("revoke_sessions", alias=alias, ok=True)
                else:
                    notes.append("⚠️ revoke вернул 0 успешных endpoints")
                    log_event("revoke_sessions", alias=alias, ok=False)
            except Exception as exc:
                LOGGER.error("revoke_all_other_sessions for %s", alias,
                             exc_info=True)
                notes.append(f"❌ revoke упал: {exc}")
                log_event("revoke_sessions", alias=alias, ok=False,
                          error=str(exc))

        if do_change_pw:
            new_pw = common.gen_password(14)
            try:
                old_pw = acc.get("password", "")
                sess.change_password(new_pw)
                self._push_previous_password(alias, old_pw)
                fresh = find_account(alias) or acc
                fresh["password"] = new_pw
                upsert_account(fresh)
                notes.append(f"✅ пароль сменён: `{new_pw}`")
                log_event("change_password", alias=alias, ok=True)
                self._track_account_success(alias)
            except Exception as exc:
                LOGGER.error("change_password for %s", alias, exc_info=True)
                notes.append(f"❌ change_password упал: {exc}")
                log_event("change_password", alias=alias, ok=False,
                          error=str(exc))
                self._track_account_failure(alias)

        if notes:
            _notify_admin(
                ctx,
                f"🔄 *autosteamrental*: пост-аренда для `{alias}`\n"
                + "\n".join(f"• {n}" for n in notes),
            )

    @staticmethod
    def _push_previous_password(alias: str, old_pw: str, limit: int = 5) -> None:
        """Сохраняет старый пароль в acc['previous_passwords'] (макс. limit)."""
        if not old_pw:
            return
        acc = find_account(alias)
        if not acc:
            return
        history = acc.get("previous_passwords") or []
        if not isinstance(history, list):
            history = []
        history.append({"password": old_pw, "ts": int(time.time())})
        if len(history) > limit:
            history = history[-limit:]
        acc["previous_passwords"] = history
        upsert_account(acc)

    @staticmethod
    def _track_account_failure(alias: str, threshold: int = 3) -> None:
        """Считает подряд идущие ошибки post-expire. После N подряд —
        замораживает аккаунт (`frozen=True`), чтобы он не выдавался дальше."""
        acc = find_account(alias)
        if not acc:
            return
        fails = int(acc.get("post_expire_fails", 0)) + 1
        acc["post_expire_fails"] = fails
        if fails >= threshold and not acc.get("frozen"):
            acc["frozen"] = True
            acc["freeze_reason"] = f"auto: {fails} post-expire failures"
            acc["freeze_ts"] = int(time.time())
            LOGGER.warning(
                "autosteamrental: автозаморозка %s после %d ошибок",
                alias, fails)
        upsert_account(acc)

    @staticmethod
    def _track_account_success(alias: str) -> None:
        acc = find_account(alias)
        if not acc:
            return
        if acc.get("post_expire_fails"):
            acc["post_expire_fails"] = 0
            upsert_account(acc)

    # --- warmup (anti-dormant) --------------------------------------------

    @staticmethod
    def _is_due_for_warmup(acc: dict[str, Any], interval_days: int) -> bool:
        """True если аккаунт давно не использовался И давно не warmup'ился."""
        if acc.get("frozen"):
            return False
        if not (acc.get("mafile") or {}).get("shared_secret"):
            return False
        if _account_in_use(acc.get("alias", "")):
            return False
        now = common.now()
        stats = acc.get("stats") or {}
        last_used = int(stats.get("last_used_at") or 0)
        last_warmup = int(stats.get("last_warmup_at") or 0)
        most_recent = max(last_used, last_warmup,
                          int(acc.get("created_at") or 0))
        if most_recent == 0:
            return True  # никогда не использовался — пора прогреть
        return (now - most_recent) >= interval_days * 24 * 3600

    def _warmup_tick(self, ctx: PluginContext) -> None:
        """Раз в `warmup_check_interval_hours` ищем 1 аккаунт под warmup
        и запускаем его в отдельном потоке. Параллельные warmup'ы
        запрещены — Steam не любит burst-логины."""
        cfg = get_config()
        if not cfg.get("warmup_enabled"):
            return
        now = common.now()
        check_int_h = max(1, int(cfg.get("warmup_check_interval_hours", 6)))
        if now - self._last_warmup_check < check_int_h * 3600:
            return
        self._last_warmup_check = now
        if self._warmup_running:
            return
        if steam_session is None:
            return
        interval_days = max(1, int(cfg.get("warmup_interval_days", 7)))
        # Кандидат: самый давно неиспользуемый из подходящих.
        candidates = [a for a in list_accounts()
                      if self._is_due_for_warmup(a, interval_days)]
        if not candidates:
            return
        candidates.sort(
            key=lambda a: int(((a.get("stats") or {}).get("last_used_at") or 0)
                              + ((a.get("stats") or {}).get("last_warmup_at") or 0)),
        )
        target = dict(candidates[0])
        idle = int(cfg.get("warmup_idle_seconds", 30))
        self._warmup_running = True
        threading.Thread(
            target=self._warmup_account,
            args=(ctx, target, idle),
            daemon=True,
            name=f"asr-warmup-{target.get('alias', '?')}",
        ).start()

    def _warmup_account(self, ctx: PluginContext,
                        acc: dict[str, Any], idle_seconds: int) -> None:
        """Выполняет один warmup-цикл для аккаунта. Обновляет stats."""
        alias = acc.get("alias", "?")
        try:
            log_event("warmup_start", alias=alias)
            ok = self._do_warmup(acc, idle_seconds)
        except Exception as exc:
            ok = False
            LOGGER.exception("autosteamrental: warmup %s failed", alias)
            log_event("warmup_exception", alias=alias, err=str(exc))
        finally:
            self._warmup_running = False
        fresh = find_account(alias)
        if not fresh:
            return
        stats = fresh.get("stats") or {}
        now = common.now()
        if ok:
            stats["last_warmup_at"] = now
            stats["warmup_count"] = int(stats.get("warmup_count", 0)) + 1
            stats["warmup_failures"] = 0
            log_event("warmup_ok", alias=alias)
        else:
            stats["warmup_failures"] = int(stats.get("warmup_failures", 0)) + 1
            stats["last_warmup_failure_at"] = now
            log_event("warmup_failed", alias=alias,
                      failures=stats["warmup_failures"])
            if stats["warmup_failures"] >= 3 and not fresh.get("frozen"):
                fresh["frozen"] = True
                fresh["freeze_reason"] = (
                    f"auto: {stats['warmup_failures']} warmup failures")
                fresh["freeze_ts"] = now
                _notify_admin(
                    ctx,
                    f"❄ *autosteamrental*: аккаунт `{alias}` заморожен "
                    f"после {stats['warmup_failures']} неудачных warmup.",
                )
        fresh["stats"] = stats
        upsert_account(fresh)

    @staticmethod
    def _do_warmup(acc: dict[str, Any], idle_seconds: int) -> bool:
        """Реальный warmup через _steam_session (login + idle)."""
        if steam_session is None:
            return False
        mafile = acc.get("mafile") or {}
        shared = mafile.get("shared_secret") or ""
        identity = mafile.get("identity_secret") or ""
        steamid = ""
        sess_block = mafile.get("Session") or {}
        if isinstance(sess_block, dict):
            steamid = str(sess_block.get("SteamID") or "")
        steamid = steamid or str(mafile.get("steamid") or "")
        login = acc.get("login") or mafile.get("account_name") or acc.get("alias", "")
        password = acc.get("password") or ""
        if not all([shared, identity, login, password]):
            return False
        sess = steam_session.SteamSession(
            account_name=login, password=password,
            shared_secret=shared, identity_secret=identity,
            steamid=steamid or None,
        )
        return bool(sess.warmup(idle_seconds=idle_seconds))

    # --- Telegram UI ------------------------------------------------------

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id

        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["autosteamrental"])
        def cmd_rental(message):
            if message.from_user.id != admin_id:
                return
            send_main(message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("asr:"))
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
                send_main(chat_id, msg_id)
            elif action == "settings":
                send_settings(chat_id, msg_id)
            elif action == "accs":
                send_accounts(chat_id, msg_id)
            elif action == "events":
                send_events(chat_id, msg_id)
            elif action == "stats":
                send_stats(chat_id, msg_id)
            elif action == "accstats":
                send_acc_stats(chat_id, msg_id)
            elif action == "instr":
                send_instruction(chat_id, msg_id)
            elif action == "active":
                send_active(chat_id, msg_id)
            elif action == "add_acc":
                wait_state[chat_id] = {"step": "wait_mafile"}
                bot.send_message(
                    chat_id,
                    "🆕 *Добавление аккаунта аренды*\n\n"
                    "Пришли `.maFile` (или ZIP с .maFile) одним сообщением. "
                    "Логин и пароль попрошу следом.\n\n"
                    "Отмена: /cancel",
                    parse_mode="Markdown",
                )
            elif action == "del_acc" and len(parts) >= 3:
                short = parts[2]
                acc = _resolve_acc(short)
                if acc:
                    delete_account(acc["alias"])
                    log_event("account_deleted", alias=acc["alias"])
                    bot.send_message(chat_id, f"🗑 Аккаунт `{common.md_escape(acc['alias'])}` удалён.",
                                     parse_mode="Markdown")
                send_accounts(chat_id)
            elif action == "view_acc" and len(parts) >= 3:
                short = parts[2]
                acc = _resolve_acc(short)
                if acc:
                    send_account_view(chat_id, acc, msg_id)
            elif action == "freeze_acc" and len(parts) >= 3:
                short = parts[2]
                acc = _resolve_acc(short)
                if acc:
                    acc["frozen"] = not acc.get("frozen", False)
                    upsert_account(acc)
                    state = "заморожен" if acc["frozen"] else "разморожен"
                    bot.send_message(chat_id, f"❄️ Аккаунт `{common.md_escape(acc['alias'])}` {state}.",
                                     parse_mode="Markdown")
                    send_account_view(chat_id, acc)
            elif action == "op" and len(parts) >= 3:
                send_op_panel(chat_id, msg_id, parts[2])
            elif action == "ext" and len(parts) >= 4:
                short, extra = parts[2], parts[3]
                try:
                    add_min = int(extra)
                except Exception:
                    add_min = 0
                if 0 < add_min <= 24 * 60:
                    found = _operator_extend(short, add_min, ctx)
                    if found:
                        bot.send_message(
                            chat_id,
                            f"➕ Аренда `{common.md_escape(found.get('alias', '?'))}` "
                            f"продлена на {add_min} мин.",
                            parse_mode="Markdown")
                send_op_panel(chat_id, msg_id, short)
            elif action == "stop" and len(parts) >= 3:
                short = parts[2]
                stopped = _operator_stop(short, ctx)
                if stopped:
                    bot.send_message(
                        chat_id,
                        f"🛑 Аренда `{common.md_escape(stopped.get('alias', '?'))}` "
                        f"прервана.", parse_mode="Markdown")
                send_active(chat_id, msg_id)
            elif action == "switch" and len(parts) >= 3:
                short = parts[2]
                result = _operator_switch_account(short, ctx)
                if result is None:
                    bot.send_message(chat_id,
                                     "❌ Не удалось сменить аккаунт.")
                elif isinstance(result, str):
                    bot.send_message(chat_id, result)
                else:
                    bot.send_message(
                        chat_id,
                        f"🔁 Аккаунт сменён на `{common.md_escape(result.get('alias', '?'))}`.",
                        parse_mode="Markdown")
                send_active(chat_id, msg_id)
            elif action == "bl" and len(parts) >= 3:
                short = parts[2]
                target = None
                for a in list_assignments():
                    if common.short_id(a.get("deal_id", "")) == short:
                        target = a
                        break
                if target is None:
                    bot.send_message(chat_id, "❌ Аренда не найдена.")
                else:
                    bv = (target.get("buyer") or "").strip().lower()
                    if bv and add_buyer_to_blacklist(bv):
                        bot.send_message(
                            chat_id,
                            f"🚫 `{common.md_escape(bv)}` добавлен в blacklist.",
                            parse_mode="Markdown")
                    else:
                        bot.send_message(chat_id, "ℹ️ Уже в blacklist.")
                send_op_panel(chat_id, msg_id, short)
            elif action == "bl_add":
                wait_state[chat_id] = {"step": "wait_blacklist"}
                bot.send_message(chat_id,
                                 "🚫 Пришли username/id/email покупателя для blacklist:")
            elif action == "bl_list":
                cfg = get_config()
                bl = cfg.get("buyer_blacklist") or []
                txt = ("🚫 *Blacklist покупателей*\n\n"
                       + ("\n".join(f"• `{common.md_escape(x)}`" for x in bl)
                          if bl else "_пусто_"))
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton(
                    "➕ Добавить", callback_data="asr:bl_add"))
                if bl:
                    kb.row(tg_types.InlineKeyboardButton(
                        "🗑 Удалить", callback_data="asr:bl_del"))
                kb.row(tg_types.InlineKeyboardButton(
                    "‹ Назад", callback_data="asr:settings"))
                _send_or_edit(bot, chat_id, msg_id, txt, kb,
                              parse_mode="Markdown")
            elif action == "bl_del":
                wait_state[chat_id] = {"step": "wait_blacklist_del"}
                bot.send_message(chat_id,
                                 "🗑 Какой покупатель удалить из blacklist?")
            elif action == "warmup_menu":
                cfg = get_config()
                wu = cfg.get("warmup_enabled", False)
                interval = int(cfg.get("warmup_interval_days", 7))
                idle = int(cfg.get("warmup_idle_seconds", 30))
                check_h = int(cfg.get("warmup_check_interval_hours", 6))
                accs = list_accounts()
                due = [a for a in accs
                       if HANDLER._is_due_for_warmup(a, interval)]
                txt = (
                    "🔥 *Warmup аккаунтов*\n\n"
                    "_Раз в N дней бот сам логинится в простаивающие "
                    "аккаунты + idle, чтобы Steam не помечал их как dormant._\n\n"
                    f"Состояние: {'🟢 включено' if wu else '🔴 выключено'}\n"
                    f"Интервал: каждые *{interval}* дн.\n"
                    f"Idle: {idle} сек.\n"
                    f"Период проверки: каждые {check_h} ч.\n\n"
                    f"Сейчас «протухших» аккаунтов: *{len(due)}*."
                )
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton(
                    ("🔴 Выключить" if wu else "🟢 Включить"),
                    callback_data="asr:toggle_warmup"))
                kb.row(tg_types.InlineKeyboardButton(
                    f"📅 Интервал: {interval} дн",
                    callback_data="asr:set_warmup_interval"))
                kb.row(tg_types.InlineKeyboardButton(
                    f"⏱ Idle: {idle} сек",
                    callback_data="asr:set_warmup_idle"))
                kb.row(tg_types.InlineKeyboardButton(
                    "‹ Назад", callback_data="asr:settings"))
                _send_or_edit(bot, chat_id, msg_id, txt, kb,
                              parse_mode="Markdown")
            elif action == "toggle_warmup":
                cfg = get_config()
                cfg["warmup_enabled"] = not cfg.get("warmup_enabled", False)
                save_config(cfg)
                bot.send_message(
                    chat_id,
                    ("✅ Warmup включён."
                     if cfg["warmup_enabled"] else "✅ Warmup выключен."))
                # Возврат к меню warmup
                call.data = "asr:warmup_menu"
                # Рендерим заново через эмуляцию
                cfg = get_config()
                wu = cfg.get("warmup_enabled", False)
                interval = int(cfg.get("warmup_interval_days", 7))
                due = [a for a in list_accounts()
                       if HANDLER._is_due_for_warmup(a, interval)]
                # просто отрендерим settings
                send_settings(chat_id, msg_id)
            elif action == "set_warmup_interval":
                wait_state[chat_id] = {"step": "wait_warmup_interval"}
                bot.send_message(
                    chat_id,
                    "📅 Сколько дней между warmup-логинами? (1–60)")
            elif action == "set_warmup_idle":
                wait_state[chat_id] = {"step": "wait_warmup_idle"}
                bot.send_message(
                    chat_id,
                    "⏱ Сколько секунд idle во время warmup? (5–600)")
            elif action == "warmup_now" and len(parts) >= 3:
                short = parts[2]
                acc = _resolve_acc(short)
                if not acc:
                    bot.send_message(chat_id, "❌ Аккаунт не найден.")
                elif acc.get("frozen"):
                    bot.send_message(chat_id, "❄ Аккаунт заморожен — пропуск.")
                elif HANDLER._warmup_running:
                    bot.send_message(chat_id,
                                     "⚠️ Уже идёт другой warmup. Подожди.")
                elif steam_session is None:
                    bot.send_message(
                        chat_id,
                        "❌ Модуль `_steam_session` не загрузился. "
                        "Установи `rsa` / `steampy`.")
                else:
                    HANDLER._warmup_running = True
                    idle = int(get_config().get("warmup_idle_seconds", 30))
                    threading.Thread(
                        target=HANDLER._warmup_account,
                        args=(ctx, dict(acc), idle),
                        daemon=True,
                        name=f"asr-warmup-manual-{acc.get('alias', '?')}",
                    ).start()
                    bot.send_message(
                        chat_id,
                        f"🔥 Warmup для `{common.md_escape(acc.get('alias', '?'))}` запущен.",
                        parse_mode="Markdown")
            elif action == "toggle_prereserve":
                cfg = get_config()
                cfg["pre_reservation_enabled"] = not cfg.get(
                    "pre_reservation_enabled", True)
                save_config(cfg)
                bot.send_message(
                    chat_id,
                    "✅ Pre-reservation "
                    + ("включено." if cfg["pre_reservation_enabled"]
                       else "выключено."))
                send_settings(chat_id, msg_id)
            elif action == "set_reminder":
                wait_state[chat_id] = {"step": "wait_reminder"}
                bot.send_message(chat_id, "За сколько минут до конца напоминать? Целое число от 0 до 240 (0 — выкл).")
            elif action == "tpl" and len(parts) >= 3:
                tpl_name = parts[2]
                wait_state[chat_id] = {"step": "wait_tpl", "tpl": tpl_name}
                cfg = get_config()
                cur = cfg.get("templates", {}).get(tpl_name, "")
                bot.send_message(
                    chat_id,
                    f"✏️ *Шаблон `{tpl_name}`*\n\nТекущий текст:\n```\n{cur}\n```\n\n"
                    "Пришли новый текст. Плейсхолдеры в `{фигурных}` сохраняй.\n"
                    "Отмена: /cancel",
                    parse_mode="Markdown",
                )

        # Ожидаемые сообщения (только для admin и только пока есть wait_state)
        @bot.message_handler(func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state,
                             content_types=["document", "text"])
        def on_wait(message):
            state = wait_state.get(message.chat.id, {})
            step = state.get("step")

            if step == "wait_mafile":
                if not message.document:
                    bot.send_message(message.chat.id, "Жду файл `.maFile` или ZIP. /cancel чтобы выйти.")
                    return
                try:
                    file_info = bot.get_file(message.document.file_id)
                    raw = bot.download_file(file_info.file_path)
                except Exception as exc:
                    bot.send_message(message.chat.id, f"Не смог скачать файл: {exc}")
                    return
                fn = (message.document.file_name or "").lower()
                pending: list[dict[str, Any]] = []
                if fn.endswith(".zip"):
                    parsed = common.parse_mafile_from_zip(raw)
                    for _name, data in parsed:
                        pending.append(data)
                else:
                    data = common.parse_mafile_bytes(raw)
                    if data:
                        pending.append(data)
                if not pending:
                    bot.send_message(message.chat.id, "Не нашёл валидных `.maFile` в файле.")
                    return
                if len(pending) == 1:
                    state["pending"] = pending
                    state["step"] = "wait_login"
                    bot.send_message(
                        message.chat.id,
                        f"✅ Загружен 1 `.maFile`: `{common.md_escape(pending[0]['account_name'] or '?')}`\n\n"
                        "Пришли логин Steam.",
                        parse_mode="Markdown",
                    )
                else:
                    added = 0
                    for data in pending:
                        alias = data.get("account_name") or f"acc_{common.now()}_{added}"
                        upsert_account({
                            "alias": alias,
                            "login": alias,
                            "password": "",
                            "mafile": data,
                            "frozen": False,
                            "added_at": common.now(),
                        })
                        added += 1
                    log_event("bulk_import", count=added)
                    wait_state.pop(message.chat.id, None)
                    bot.send_message(
                        message.chat.id,
                        f"✅ Импортировано {added} аккаунтов из ZIP.\n"
                        "Пароли пустые — задай вручную через меню аккаунта.",
                    )
                    send_accounts(message.chat.id)
                return

            if step == "wait_login":
                login = (message.text or "").strip()
                if not login:
                    return
                state["login"] = login
                state["step"] = "wait_password"
                bot.send_message(message.chat.id, "Пришли пароль Steam.")
                return

            if step == "wait_password":
                password = (message.text or "").strip()
                if not password:
                    return
                data = state["pending"][0]
                alias = data.get("account_name") or state["login"]
                upsert_account({
                    "alias": alias,
                    "login": state["login"],
                    "password": password,
                    "mafile": data,
                    "frozen": False,
                    "added_at": common.now(),
                })
                log_event("account_added", alias=alias)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id,
                                 f"✅ Аккаунт `{common.md_escape(alias)}` добавлен.",
                                 parse_mode="Markdown")
                send_accounts(message.chat.id)
                return

            if step == "wait_reminder":
                txt = (message.text or "").strip()
                try:
                    n = int(txt)
                    assert 0 <= n <= 240
                except Exception:
                    bot.send_message(message.chat.id, "Целое число от 0 до 240.")
                    return
                cfg = get_config()
                cfg["reminder_minutes_before"] = n
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"✅ Напоминание: за {n} мин.")
                send_settings(message.chat.id)
                return

            if step == "wait_blacklist":
                txt = (message.text or "").strip()
                if not txt:
                    return
                wait_state.pop(message.chat.id, None)
                if add_buyer_to_blacklist(txt):
                    bot.send_message(
                        message.chat.id,
                        f"🚫 `{common.md_escape(txt.lower())}` добавлен.",
                        parse_mode="Markdown")
                else:
                    bot.send_message(message.chat.id, "ℹ️ Уже в списке.")
                return

            if step == "wait_warmup_interval":
                txt = (message.text or "").strip()
                try:
                    n = int(txt)
                    assert 1 <= n <= 60
                except Exception:
                    bot.send_message(message.chat.id,
                                     "Целое число 1–60.")
                    return
                cfg = get_config()
                cfg["warmup_interval_days"] = n
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id,
                                 f"✅ Интервал warmup: {n} дн.")
                send_settings(message.chat.id)
                return

            if step == "wait_warmup_idle":
                txt = (message.text or "").strip()
                try:
                    n = int(txt)
                    assert 5 <= n <= 600
                except Exception:
                    bot.send_message(message.chat.id,
                                     "Целое число 5–600.")
                    return
                cfg = get_config()
                cfg["warmup_idle_seconds"] = n
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id,
                                 f"✅ Idle warmup: {n} сек.")
                send_settings(message.chat.id)
                return

            if step == "wait_blacklist_del":
                txt = (message.text or "").strip()
                if not txt:
                    return
                wait_state.pop(message.chat.id, None)
                if remove_buyer_from_blacklist(txt):
                    bot.send_message(
                        message.chat.id,
                        f"🗑 `{common.md_escape(txt.lower())}` удалён.",
                        parse_mode="Markdown")
                else:
                    bot.send_message(message.chat.id, "ℹ️ Нет такого.")
                return

            if step == "wait_tpl":
                tpl_name = state.get("tpl")
                new_text = message.text or ""
                if not new_text.strip():
                    return
                cfg = get_config()
                cfg.setdefault("templates", {})[tpl_name] = new_text
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, "✅ Шаблон обновлён.")
                send_settings(message.chat.id)
                return

        # Экраны меню
        def send_main(chat_id: int, edit_msg_id: int | None = None):
            tg_id = admin_id
            sess = "********"
            text = (
                "🎮 *Меню autosteamrental*\n\n"
                "*autosteamrental v1.0*\n"
                "Модуль, автоматизирующий аренду Steam аккаунтов. "
                "`/autosteamrental` в Telegram-боте для управления.\n\n"
                "Ваша лицензия:\n"
                f"• session\\_token: `{sess}`\n"
                f"• telegram\\_id: `{tg_id}`\n\n"
                "Ссылки:\n"
                "• [@alleexxeeyy](https://t.me/alleexxeeyy) — разработчик PlayerokAPI\n"
                "• [@alexey\\_production\\_bot](https://t.me/alexey_production_bot) — бот для покупки модулей\n\n"
                "Перемещайтесь по разделам ниже ↓"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton("⚙ Настройки", callback_data="asr:settings"),
                tg_types.InlineKeyboardButton("🎮 Активные", callback_data="asr:active"),
                tg_types.InlineKeyboardButton("👤 Аккаунты", callback_data="asr:accs"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("🚩 Ивенты", callback_data="asr:events"),
                tg_types.InlineKeyboardButton("📊 Статистика", callback_data="asr:stats"),
            )
            kb.row(tg_types.InlineKeyboardButton("📖 Инструкция", callback_data="asr:instr"))
            kb.row(
                tg_types.InlineKeyboardButton("🧑‍💻 Разработчик", url="https://t.me/alleexxeeyy"),
                tg_types.InlineKeyboardButton("🤖 Наш бот", url="https://t.me/alexey_production_bot"),
            )
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown",
                          disable_web_page_preview=True)

        def send_settings(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            rmin = cfg.get("reminder_minutes_before", 15)
            auto_parse = "✅" if cfg.get("auto_parse_duration", True) else "❌"
            fb = "✅" if cfg.get("fallback_any_account", True) else "❌"
            alert = "✅" if cfg.get("alert_no_accounts", True) else "❌"
            text = (
                "⚙ *Настройки autosteamrental*\n\n"
                f"• Напоминание за: *{rmin} мин* до конца\n"
                f"• Авто-парсинг длительности из названия: {auto_parse}\n"
                f"• Брать любой аккаунт если нет под lot: {fb}\n"
                f"• Алерт при отсутствии аккаунтов: {alert}\n\n"
                "Шаблоны сообщений ↓"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                f"⏰ Напоминание: {rmin} мин", callback_data="asr:set_reminder"))
            bl_count = len(cfg.get("buyer_blacklist") or [])
            pre = cfg.get("pre_reservation_enabled", True)
            kb.row(tg_types.InlineKeyboardButton(
                f"🚫 Blacklist ({bl_count})", callback_data="asr:bl_list"))
            kb.row(tg_types.InlineKeyboardButton(
                f"⏳ Pre-reservation: {'🟢' if pre else '🔴'} "
                f"{int(cfg.get('pre_reservation_minutes', 5))} мин",
                callback_data="asr:toggle_prereserve"))
            wu = cfg.get("warmup_enabled", False)
            kb.row(tg_types.InlineKeyboardButton(
                f"🔥 Warmup: {'🟢' if wu else '🔴'} "
                f"раз в {int(cfg.get('warmup_interval_days', 7))} дн",
                callback_data="asr:warmup_menu"))
            for tpl in ("issue", "guard_code_first", "guard_code",
                        "expired", "reminder",
                        "guard_no_rental", "guard_wrong_alias", "no_accounts"):
                kb.row(tg_types.InlineKeyboardButton(
                    f"✏️ Шаблон: {tpl}", callback_data=f"asr:tpl:{tpl}"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_accounts(chat_id: int, edit_msg_id: int | None = None):
            accs = list_accounts()
            free = sum(1 for a in accs if not a.get("frozen") and not _account_in_use(a.get("alias", "")))
            in_use = sum(1 for a in accs if _account_in_use(a.get("alias", "")))
            frozen = sum(1 for a in accs if a.get("frozen"))
            text = (
                "👤 *Аккаунты Steam (аренда)*\n"
                f"Всего: {len(accs)} | Свободно: {free} | В работе: {in_use} | Заморожено: {frozen}\n\n"
                "Нажмите на аккаунт, чтобы перейти в его редактирование ↓"
            )
            kb = tg_types.InlineKeyboardMarkup()
            for a in accs[:30]:
                alias = a.get("alias", "?")
                pwd = a.get("password", "") or ""
                pwd_show = (pwd[:3] + "*****") if pwd else "(нет)"
                in_use_str = " · занят" if _account_in_use(alias) else ""
                frozen_str = " · ❄" if a.get("frozen") else ""
                kb.row(tg_types.InlineKeyboardButton(
                    f"{alias} | {pwd_show}{in_use_str}{frozen_str}",
                    callback_data=f"asr:view_acc:{common.short_id(alias)}",
                ))
            kb.row(tg_types.InlineKeyboardButton("➕ Добавить", callback_data="asr:add_acc"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_account_view(chat_id: int, acc: dict[str, Any], edit_msg_id: int | None = None):
            alias = acc.get("alias", "?")
            mf = acc.get("mafile", {})
            state = "❄ Заморожен" if acc.get("frozen") else ("⏳ В аренде" if _account_in_use(alias) else "✅ Свободен")
            stats = acc.get("stats") or {}
            last_wu = int(stats.get("last_warmup_at") or 0)
            wu_line = (
                f"🔥 Warmup: {common.fmt_ts(last_wu) if last_wu else '—'} · "
                f"всего {int(stats.get('warmup_count', 0))}"
                + (f" · ошибок подряд {int(stats.get('warmup_failures', 0))}"
                   if stats.get("warmup_failures") else "")
            )
            text = (
                "📝 *Редактирование аккаунта аренды Steam*\n\n"
                f"👀 Состояние: *{state}*\n"
                f"👤 Логин: `{common.md_escape(acc.get('login', ''))}`\n"
                f"🔑 Пароль: `{common.md_escape(_mask(acc.get('password', '')))}`\n\n"
                "🗒 maFile данные:\n"
                f"• shared\\_secret: `{_mask(mf.get('shared_secret', ''))}`\n"
                f"• identity\\_secret: `{_mask(mf.get('identity_secret', ''))}`\n"
                f"• device\\_id: `{_mask(mf.get('device_id', ''))}`\n"
                f"• steam\\_id: `{common.md_escape(mf.get('steam_id', ''))}`\n\n"
                f"{wu_line}\n"
            )
            sid = common.short_id(alias)
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "❄ Заморозить/Разморозить", callback_data=f"asr:freeze_acc:{sid}"))
            kb.row(tg_types.InlineKeyboardButton(
                "🔥 Warmup сейчас", callback_data=f"asr:warmup_now:{sid}"))
            kb.row(tg_types.InlineKeyboardButton("🗑 Удалить аккаунт", callback_data=f"asr:del_acc:{sid}"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:accs"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_active(chat_id: int, edit_msg_id: int | None = None):
            items = [a for a in list_assignments()
                     if a.get("status") in ("active", "delivered", "reserved")]
            text = f"🎮 *Активные аренды ({len(items)})*\n\n"
            if not items:
                text += "Сейчас активных аренд нет."
            else:
                now_ts = common.now()
                for a in items[:30]:
                    status = a.get("status")
                    if status == "reserved":
                        left = max(0, int(a.get("reserved_until", 0)) - now_ts)
                        timer = f"резерв {common.human_seconds(left)}"
                    elif status == "delivered":
                        timer = "ожидает первого `!steamguard`"
                    else:
                        ends = int(a.get("expires_at", 0) or 0)
                        left = max(0, ends - now_ts)
                        timer = common.human_seconds(left)
                    text += (
                        f"• `{common.md_escape(a.get('alias', '?'))}` · "
                        f"{common.md_escape(a.get('buyer', '?'))} · ⏳ {timer}\n"
                    )
            kb = tg_types.InlineKeyboardMarkup()
            # Кнопки действий по каждой аренде (только active/delivered).
            actionable = [a for a in items
                          if a.get("status") in ("active", "delivered")][:6]
            for a in actionable:
                did = common.short_id(a.get("deal_id", ""))
                alias = a.get("alias", "?")
                kb.row(tg_types.InlineKeyboardButton(
                    f"⚙ {alias} · {a.get('buyer', '?')}",
                    callback_data=f"asr:op:{did}",
                ))
            kb.row(tg_types.InlineKeyboardButton("🔄 Обновить", callback_data="asr:active"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_op_panel(chat_id: int, edit_msg_id: int | None,
                          deal_short: str):
            target = None
            for a in list_assignments():
                if common.short_id(a.get("deal_id", "")) == deal_short:
                    target = a
                    break
            if target is None:
                _send_or_edit(bot, chat_id, edit_msg_id,
                              "❌ Аренда не найдена.", None)
                return
            alias = target.get("alias", "?")
            status = target.get("status", "?")
            now_ts = common.now()
            ends = int(target.get("expires_at", 0) or 0)
            left = max(0, ends - now_ts) if ends else 0
            text = (
                f"⚙ *Управление арендой*\n\n"
                f"🔑 Аккаунт: `{common.md_escape(alias)}`\n"
                f"👤 Покупатель: `{common.md_escape(target.get('buyer', '?'))}`\n"
                f"📦 Товар: {common.md_escape(target.get('item_name', '?'))}\n"
                f"📊 Статус: `{status}`\n"
                f"⏳ Осталось: {common.human_seconds(left) if left else '—'}\n"
                f"🆔 `{target.get('deal_id', '')}`"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "➕15м", callback_data=f"asr:ext:{deal_short}:15"),
                tg_types.InlineKeyboardButton(
                    "➕30м", callback_data=f"asr:ext:{deal_short}:30"),
                tg_types.InlineKeyboardButton(
                    "➕60м", callback_data=f"asr:ext:{deal_short}:60"),
            )
            kb.row(tg_types.InlineKeyboardButton(
                "🛑 Прервать", callback_data=f"asr:stop:{deal_short}"))
            kb.row(tg_types.InlineKeyboardButton(
                "🔁 Сменить аккаунт", callback_data=f"asr:switch:{deal_short}"))
            kb.row(tg_types.InlineKeyboardButton(
                "🚫 Buyer → blacklist",
                callback_data=f"asr:bl:{deal_short}"))
            kb.row(tg_types.InlineKeyboardButton(
                "‹ Назад", callback_data="asr:active"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb,
                          parse_mode="Markdown")

        def send_events(chat_id: int, edit_msg_id: int | None = None):
            evs = common.load_json(EVENTS_FILE, {})
            last = evs.get("last_notify_unclosed") or 0
            nxt = (last + 24 * 3600) if last else 0
            text = (
                "🚩 *Ивенты autosteamrental*\n\n"
                "🔔 Уведомление незакрытых заказов:\n"
                f"• Последнее: {common.fmt_ts(last) if last else '—'}\n"
                f"• Следующее: {common.fmt_ts(nxt) if nxt else '—'}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_stats(chat_id: int, edit_msg_id: int | None = None):
            history = common.load_json(HISTORY_FILE, [])
            delivered = sum(1 for h in history if h.get("event") == "delivered")
            started = sum(1 for h in history if h.get("event") == "rental_started")
            expired = sum(1 for h in history if h.get("event") == "rental_expired")
            codes = sum(1 for h in history if h.get("event") in ("rental_started", "guard_issued"))
            active = sum(1 for a in list_assignments() if a.get("status") in ("active", "delivered"))
            text = (
                "📊 *Статистика autosteamrental*\n\n"
                "Статистика с момента запуска:\n"
                f"• Выполнено заказов на аренду: {delivered}\n"
                f"• Запущенных аренд (после первого !steamguard): {started}\n"
                f"• Истёкших аренд: {expired}\n"
                f"• Активных сейчас: {active}\n"
                f"• Steam Guard кодов выдано: {codes}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("📈 По аккаунтам", callback_data="asr:accstats"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_acc_stats(chat_id: int, edit_msg_id: int | None = None):
            accs = list_accounts()
            if not accs:
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:stats"))
                _send_or_edit(bot, chat_id, edit_msg_id, "📭 Нет аккаунтов.", kb)
                return
            lines: list[str] = ["📈 *Per-account аналитика*\n"]
            for a in accs:
                st = a.get("stats") or {}
                rents = int(st.get("rentals_count", 0))
                dlv = int(st.get("delivered_count", 0))
                total_min = int(st.get("total_minutes", 0))
                rev = float(st.get("total_revenue", 0))
                alias = a.get("alias", "?")
                h, m = divmod(total_min, 60)
                time_str = f"{h}ч {m}м" if h else f"{m}м"
                line = (
                    f"🎮 `{alias}`\n"
                    f"   выдано: {dlv} | аренд: {rents} | время: {time_str}"
                )
                if rev > 0:
                    line += f" | выручка: {rev:.0f}₽"
                if a.get("frozen"):
                    line += " ❄️"
                lines.append(line)
            text = "\n".join(lines)
            if len(text) > 3900:
                text = text[:3900] + "\n…"
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:stats"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_instruction(chat_id: int, edit_msg_id: int | None = None):
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="asr:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, PLUGIN.instruction, kb, parse_mode="Markdown")

        def _resolve_acc(short: str) -> dict[str, Any] | None:
            for a in list_accounts():
                if common.short_id(a.get("alias", "")) == short:
                    return a
            return None


HANDLER = Handler()


# ─── Telegram helpers ────────────────────────────────────────────

def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 5:
        return "*****"
    return s[:5] + "*****"


def _send_or_edit(bot, chat_id: int, msg_id: int | None, text: str,
                  kb: tg_types.InlineKeyboardMarkup | None = None,
                  parse_mode: str | None = None,
                  disable_web_page_preview: bool | None = None) -> None:
    try:
        if msg_id:
            bot.edit_message_text(
                text, chat_id, msg_id,
                parse_mode=parse_mode, reply_markup=kb,
                disable_web_page_preview=bool(disable_web_page_preview),
            )
            return
    except Exception:
        pass
    try:
        bot.send_message(
            chat_id, text, parse_mode=parse_mode, reply_markup=kb,
            disable_web_page_preview=bool(disable_web_page_preview),
        )
    except Exception:
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            LOGGER.debug("autosteamrental: send_message fallback failed", exc_info=True)
