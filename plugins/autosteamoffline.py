"""Плагин autosteamoffline — продажа Steam-аккаунтов навсегда (офлайн-активация)
с лимитом выдач Steam Guard кода через команду `!guard` в чате Playerok.

Поведение:
  * Покупатель оплачивает товар → плагин берёт свободный аккаунт из пула,
    отправляет в чат Playerok логин/пароль и инструкцию по команде `!guard`.
  * Покупатель пишет `!guard` (или `!код` — алиас для обратной совместимости) →
    плагин генерирует Steam Guard-код через steampy и шлёт его в чат, уменьшая
    счётчик оставшихся выдач у этой выдачи (assignment).
  * После исчерпания лимита бот отвечает шаблоном `guard_limit_reached`.

Управление через Telegram-команду `/autosteamoffline` (inline-меню) или кнопку
из главного меню бота.

Хранилище — `storage/plugins/autosteamoffline/{accounts,assignments,history,
events,config}.json`. Не пересекается с autosteamrental.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.autosteamoffline")
STORAGE_DIR = os.path.join("storage", "plugins", "autosteamoffline")
ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")
ASSIGNMENTS_FILE = os.path.join(STORAGE_DIR, "assignments.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")
EVENTS_FILE = os.path.join(STORAGE_DIR, "events.json")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")


# ─── Шаблоны ─────────────────────────────────────────────────────

DEFAULT_TEMPLATES: dict[str, str] = {
    "issue": (
        "🛡 Спасибо за покупку оффлайн активации Steam!\n\n"
        "Ваши данные:\n"
        "• Логин: {login}\n"
        "• Пароль: {password}\n\n"
        "Для получения кода, отправьте в чат команду !guard\n"
        "Вы можете получать код {codes_limit} раз(-а)\n\n"
        "💡 Если нужна помощь, позовите продавца командой !продавец, и он вам поможет"
    ),
    "guard_code": (
        "🔐 Код SteamGuard: {code}\n"
        "❗ Код действителен 30 секунд"
    ),
    "guard_limit_reached": (
        "❌ Лимит выдач кода исчерпан. Обратитесь к продавцу командой !продавец."
    ),
    "guard_no_assignment": (
        "ℹ️ Эта команда сработает только после покупки аккаунта."
    ),
    "no_accounts": (
        "❌ Извините, сейчас нет свободных аккаунтов. Продавцу отправлено уведомление."
    ),
    "seller_help": (
        "📞 Продавец уже уведомлён, он скоро ответит."
    ),
}


DEFAULT_CONFIG: dict[str, Any] = {
    # Сколько кодов покупатель может получить по умолчанию.
    "default_codes_limit": 3,
    # Соответствие конкретного Playerok item_id → параметрам выдачи.
    # Пример: {"itemId123": {"codes_limit": 5, "aliases": ["acc1"]}}
    "lot_map": {},
    # Если True — плагин пытается обработать ЛЮБУЮ оплату (а не только из
    # lot_map), беря любой свободный аккаунт. Для строгого режима — False.
    "auto_match_any_deal": True,
    # Передаваемые шаблоны.
    "templates": dict(DEFAULT_TEMPLATES),
    # Поднять алерт админу, если на оплату не нашлось свободного аккаунта.
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
    al = alias.lower()
    for a in list_assignments():
        if a.get("alias", "").lower() == al and a.get("status") == "active":
            return True
    return False


def pick_free_account(preferred_aliases: list[str] | None = None) -> dict[str, Any] | None:
    accs = list_accounts()
    pool = accs
    if preferred_aliases:
        prefs = {a.lower() for a in preferred_aliases}
        pool = [a for a in accs if a.get("alias", "").lower() in prefs]
        if not pool:
            pool = accs  # fallback на весь пул
    for a in pool:
        if a.get("frozen"):
            continue
        if _account_in_use(a.get("alias", "")):
            continue
        return a
    return None


# ─── Хранилище выдач (assignments) ───────────────────────────────

def list_assignments() -> list[dict[str, Any]]:
    return common.load_json(ASSIGNMENTS_FILE, [])


def save_assignments(items: list[dict[str, Any]]) -> None:
    common.save_json(ASSIGNMENTS_FILE, items)


def find_assignment_by_chat(chat_id: str) -> dict[str, Any] | None:
    chat_id = str(chat_id)
    # Берём последнюю активную выдачу в этом чате.
    items = [a for a in list_assignments()
             if str(a.get("chat_id")) == chat_id and a.get("status") == "active"]
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


# ─── История ─────────────────────────────────────────────────────

def log_event(event: str, **extra: Any) -> None:
    entry = {"ts": common.now(), "event": event}
    entry.update({k: v for k, v in extra.items() if v is not None})
    history = common.load_json(HISTORY_FILE, [])
    history.append(entry)
    if len(history) > 10000:
        history = history[-10000:]
    common.save_json(HISTORY_FILE, history)


# ─── Метаданные плагина ──────────────────────────────────────────

PLUGIN = Plugin(
    id="autosteamoffline",
    name="autosteamoffline",
    icon="🛡",
    description=(
        "Модуль, автоматизирующий продажу оффлайн активаций Steam. "
        "/autosteamoffline в Telegram-боте для управления."
    ),
    instruction=(
        "*🛡 autosteamoffline*\n\n"
        "*Что делает плагин:*\n"
        "• После оплаты товара бот сам отправляет в чат логин/пароль и "
        "инструкцию: `!guard` — получить Steam Guard код.\n"
        "• Команда `!guard` в чате Playerok отдаёт код покупателю, лимит "
        "выдач задаётся в настройках (по умолчанию 3).\n"
        "• Все аккаунты с `.maFile` управляются через `/autosteamoffline` → "
        "«Аккаунты». Один аккаунт = одна выдача.\n\n"
        "*Как настроить:*\n"
        "1. Включи плагин кнопкой ниже.\n"
        "2. `/autosteamoffline` → «Аккаунты» → «Добавить» → пришли `.maFile` "
        "(или ZIP со всеми) + логин/пароль.\n"
        "3. В «Настройках» можно изменить лимит выдач и шаблоны.\n\n"
        "*Команды покупателю:*\n"
        "• `!guard` — получить Steam Guard код.\n"
        "• `!продавец` — позвать продавца."
    ),
    default_enabled=True,
    keywords=("!guard", "оффлайн", "offline", "steam"),
)


# ─── Helper для отправки в Playerok ──────────────────────────────

def _safe_send(ctx: PluginContext, chat_id: str, text: str) -> bool:
    if not ctx.playerok_acc:
        ctx.log.warning("autosteamoffline: playerok_acc is None, не могу отправить в чат %s", chat_id)
        return False
    try:
        ctx.playerok_acc.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:
        ctx.log.error("autosteamoffline: send_message failed: %s", exc)
        return False


def _notify_admin(ctx: PluginContext, text: str, parse_mode: str | None = "Markdown") -> None:
    try:
        ctx.bot.send_message(ctx.admin_id, text, parse_mode=parse_mode)
    except Exception:
        try:
            ctx.bot.send_message(ctx.admin_id, text)
        except Exception:
            ctx.log.debug("autosteamoffline: admin notify failed", exc_info=True)


def _looks_offline_item(item_name: str, comment: str | None = None) -> bool:
    """Эвристика: похоже ли название/комментарий на оффлайн-активацию."""
    if not item_name:
        return False
    s = (item_name + " " + (comment or "")).lower()
    return any(kw in s for kw in (
        "оффлайн", "офлайн", "offline", "навсегда", "перм", "perm"
    ))


# ─── Главный обработчик событий ──────────────────────────────────

class Handler:
    """Логика плагина: события Playerok + Telegram-меню."""

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
            # ITEM_PAID обычно приходит раньше; NEW_DEAL — fallback.
            return self._handle_item_paid(event, ctx)
        if etype is EventTypes.NEW_MESSAGE:
            return self._handle_new_message(event, ctx)
        return False

    def _handle_item_paid(self, event: Any, ctx: PluginContext) -> bool:
        deal = getattr(event, "deal", None)
        if deal is None:
            return False
        deal_id = getattr(deal, "id", None)
        if not deal_id:
            return False

        # Не обрабатываем повторно одну и ту же сделку.
        if find_assignment_by_deal(deal_id):
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
            if not cfg.get("auto_match_any_deal", True):
                return False  # строгий режим — берём только из lot_map
            # Авто-режим: пробуем определить по ключевым словам, чтобы не
            # перехватить аренду autosteamrental.
            comment = getattr(item, "description", None) or getattr(deal, "comment_from_buyer", None)
            if not _looks_offline_item(item_name, comment):
                # Если есть autosteamrental, пусть он разбирается; иначе мы
                # всё равно подхватываем (заказы без характеризующих слов).
                from . import is_enabled
                if is_enabled("autosteamrental", ctx.get_config()):
                    return False

        codes_limit = (lot or {}).get("codes_limit") or int(cfg.get("default_codes_limit", 3))
        preferred = (lot or {}).get("aliases") or None
        acc = pick_free_account(preferred)
        if acc is None:
            _safe_send(ctx, chat_id, render_template("no_accounts"))
            if cfg.get("alert_no_accounts", True):
                _notify_admin(
                    ctx,
                    f"⚠️ *autosteamoffline*: нет свободных аккаунтов под заказ\n"
                    f"🛒 {common.md_escape(item_name)}\n🆔 `{deal_id}`",
                )
            log_event("no_accounts", deal_id=deal_id, item=item_name)
            return True

        buyer = getattr(getattr(deal, "user", None), "username", None) or "?"

        text = render_template(
            "issue",
            login=acc.get("login", ""),
            password=acc.get("password", ""),
            codes_limit=codes_limit,
            game=item_name or "Steam",
            alias=acc.get("alias", ""),
        )
        if not _safe_send(ctx, chat_id, text):
            _notify_admin(
                ctx,
                f"❌ *autosteamoffline*: не удалось отправить выдачу в чат\n"
                f"🆔 `{deal_id}`",
            )
            return True

        add_assignment({
            "deal_id": deal_id,
            "alias": acc.get("alias", ""),
            "buyer": buyer,
            "chat_id": str(chat_id),
            "item_id": item_id,
            "item_name": item_name,
            "codes_used": 0,
            "codes_limit": int(codes_limit),
            "created_at": common.now(),
            "status": "active",
        })
        log_event("issued", deal_id=deal_id, alias=acc.get("alias"),
                  buyer=buyer, codes_limit=codes_limit)
        _notify_admin(
            ctx,
            f"🛡 *autosteamoffline*: выдан аккаунт\n"
            f"🛒 {common.md_escape(item_name)}\n"
            f"👤 {common.md_escape(buyer)}\n"
            f"🔑 `{acc.get('alias', '')}`\n"
            f"🔢 Кодов: {codes_limit}\n"
            f"🆔 `{deal_id}`",
        )
        return True

    def _handle_new_message(self, event: Any, ctx: PluginContext) -> bool:
        msg = getattr(event, "message", None)
        if msg is None:
            return False
        user = getattr(msg, "user", None)
        if user is None or not ctx.playerok_acc:
            return False
        # Игнорируем свои сообщения.
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

        if low.startswith("!guard") or low.startswith("!код") or low.startswith("!code"):
            return self._cmd_guard(chat_id, ctx)
        return False

    def _cmd_guard(self, chat_id: str, ctx: PluginContext) -> bool:
        assignment = find_assignment_by_chat(str(chat_id))
        if assignment is None:
            # Чтобы не конфликтовать с autosteamrental, если он включён —
            # пропускаем команду (это его !steamguard, не наш). Но !guard
            # без активной выдачи отдаём с подсказкой.
            from . import is_enabled
            if is_enabled("autosteamrental", ctx.get_config()):
                return False
            _safe_send(ctx, str(chat_id), render_template("guard_no_assignment"))
            return True

        codes_used = int(assignment.get("codes_used", 0))
        codes_limit = int(assignment.get("codes_limit", 3))
        if codes_used >= codes_limit:
            _safe_send(ctx, str(chat_id), render_template("guard_limit_reached"))
            log_event("guard_limit_reached",
                      deal_id=assignment.get("deal_id"),
                      alias=assignment.get("alias"))
            return True

        acc = find_account(assignment.get("alias", ""))
        if acc is None:
            _safe_send(ctx, str(chat_id), "❌ Внутренняя ошибка: аккаунт удалён.")
            return True

        code = common.generate_code(acc.get("mafile", {}).get("shared_secret", ""))
        if not code:
            _safe_send(ctx, str(chat_id), "❌ Не удалось сгенерировать код. Обратитесь к продавцу.")
            return True

        # +1 к счётчику
        codes_used += 1
        update_assignment(assignment["deal_id"], codes_used=codes_used)

        codes_left = codes_limit - codes_used
        body = render_template(
            "guard_code",
            code=code,
            codes_left=codes_left,
            codes_limit=codes_limit,
            seconds=common.seconds_until_code_change(),
        )
        if codes_left > 0:
            body += f"\n🔢 Осталось выдач: {codes_left} из {codes_limit}"
        else:
            body += "\n⚠️ Это была последняя выдача кода."
        _safe_send(ctx, str(chat_id), body)
        log_event("guard_issued",
                  deal_id=assignment.get("deal_id"),
                  alias=assignment.get("alias"),
                  codes_used=codes_used, codes_limit=codes_limit)
        return True

    # --- Telegram UI ------------------------------------------------------

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id

        # Состояние «ожидаем .maFile / логин / пароль ...» по chat_id.
        # Простое key=value хранилище в памяти процесса.
        wait_state: dict[int, dict[str, Any]] = {}

        # ── /autosteamoffline ──
        @bot.message_handler(commands=["autosteamoffline"])
        def cmd_offline(message):
            if message.from_user.id != admin_id:
                return
            send_main(message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("aso:"))
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
            elif action == "instr":
                send_instruction(chat_id, msg_id)
            elif action == "add_acc":
                wait_state[chat_id] = {"step": "wait_mafile"}
                bot.send_message(
                    chat_id,
                    "🆕 *Добавление аккаунта*\n\n"
                    "Пришли `.maFile` (или ZIP с .maFile) одним сообщением. "
                    "Логин и пароль попрошу следом.\n\n"
                    "Отмена: /cancel",
                    parse_mode="Markdown",
                )
            elif action == "del_acc" and len(parts) >= 3:
                # aso:del_acc:<short_id>
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
            elif action == "set_limit":
                wait_state[chat_id] = {"step": "wait_limit"}
                bot.send_message(chat_id, "Сколько раз покупатель может получить код? Пришли число (1-99).")
            elif action == "tpl" and len(parts) >= 3:
                tpl_name = parts[2]
                wait_state[chat_id] = {"step": "wait_tpl", "tpl": tpl_name}
                cfg = get_config()
                cur = cfg.get("templates", {}).get(tpl_name, "")
                bot.send_message(
                    chat_id,
                    f"✏️ *Шаблон `{tpl_name}`*\n\nТекущий текст:\n```\n{cur}\n```\n\n"
                    "Пришли новый текст. Плейсхолдеры в `{фигурных}` сохраняй как есть.\n"
                    "Отмена: /cancel",
                    parse_mode="Markdown",
                )

        @bot.message_handler(commands=["cancel"])
        def cancel(message):
            if message.from_user.id != admin_id:
                return
            wait_state.pop(message.chat.id, None)
            bot.send_message(message.chat.id, "Отменено.")

        # Обработка ожидаемого ввода — .maFile / логин / пароль / лимит / шаблон.
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
                        "Пришли логин Steam одним сообщением.",
                        parse_mode="Markdown",
                    )
                else:
                    # Bulk-режим: passwords.txt не было — заводим всё с пустыми паролями
                    # и сообщаем, что их можно отредактировать.
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
                        "Пароли не были найдены — задай их вручную через "
                        "/autosteamoffline → Аккаунты → выбрать → «Изменить пароль».",
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
                bot.send_message(
                    message.chat.id,
                    f"✅ Аккаунт `{common.md_escape(alias)}` добавлен.",
                    parse_mode="Markdown",
                )
                send_accounts(message.chat.id)
                return

            if step == "wait_limit":
                txt = (message.text or "").strip()
                try:
                    n = int(txt)
                    assert 1 <= n <= 99
                except Exception:
                    bot.send_message(message.chat.id, "Нужно целое число от 1 до 99.")
                    return
                cfg = get_config()
                cfg["default_codes_limit"] = n
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"✅ Лимит выдач: {n}.")
                send_settings(message.chat.id)
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

        # ── Меню/экраны ──
        def send_main(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            tg_id = admin_id
            sess = "********"  # для красоты — как на скрине
            text = (
                "🛡 *Меню autosteamoffline*\n\n"
                "*autosteamoffline v1.1.5*\n"
                "Модуль, автоматизирующий продажу оффлайн активаций Steam. "
                "`/autosteamoffline` в Telegram-боте для управления.\n\n"
                "Ваша лицензия:\n"
                f"• session\\_token: `{sess}`\n"
                f"• telegram\\_id: `{tg_id}`\n\n"
                "Ссылки:\n"
                "• [@alleexxeeyy](https://t.me/alleexxeeyy) — разработчик PlayerokAPI\n"
                "• [@alexey\\_production\\_bot](https://t.me/alexey_production_bot) — бот для покупки модулей\n\n"
                "Перемещайтесь по разделам ниже ↓"
            )
            del cfg  # просто чтобы не «не используется»
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton("⚙ Настройки", callback_data="aso:settings"),
                tg_types.InlineKeyboardButton("👤 Аккаунты", callback_data="aso:accs"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("🚩 Ивенты", callback_data="aso:events"),
                tg_types.InlineKeyboardButton("📊 Статистика", callback_data="aso:stats"),
            )
            kb.row(tg_types.InlineKeyboardButton("📖 Инструкция", callback_data="aso:instr"))
            kb.row(
                tg_types.InlineKeyboardButton("🧑‍💻 Разработчик", url="https://t.me/alleexxeeyy"),
                tg_types.InlineKeyboardButton("🤖 Наш бот", url="https://t.me/alexey_production_bot"),
            )
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown",
                          disable_web_page_preview=True)

        def send_settings(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            limit = cfg.get("default_codes_limit", 3)
            auto = "✅" if cfg.get("auto_match_any_deal", True) else "❌"
            alert = "✅" if cfg.get("alert_no_accounts", True) else "❌"
            text = (
                "⚙ *Настройки autosteamoffline*\n\n"
                f"• Лимит выдач кода: *{limit}* раз(-а)\n"
                f"• Авто-подхват любых оплат: {auto}\n"
                f"• Алерт при отсутствии аккаунтов: {alert}\n\n"
                "Шаблоны сообщений ниже ↓"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(f"🔢 Лимит: {limit}", callback_data="aso:set_limit"),
            )
            for tpl in ("issue", "guard_code", "guard_limit_reached",
                        "guard_no_assignment", "no_accounts"):
                kb.row(tg_types.InlineKeyboardButton(
                    f"✏️ Шаблон: {tpl}", callback_data=f"aso:tpl:{tpl}"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_accounts(chat_id: int, edit_msg_id: int | None = None):
            accs = list_accounts()
            free = sum(1 for a in accs if not a.get("frozen") and not _account_in_use(a.get("alias", "")))
            in_use = sum(1 for a in accs if _account_in_use(a.get("alias", "")))
            frozen = sum(1 for a in accs if a.get("frozen"))
            text = (
                "👤 *Аккаунты Steam (оффлайн)*\n"
                f"Всего: {len(accs)} | Свободно: {free} | В работе: {in_use} | Заморожено: {frozen}\n\n"
                "Перемещайтесь по разделам ниже. Нажмите на аккаунт, чтобы перейти в его редактирование ↓"
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
                    callback_data=f"aso:view_acc:{common.short_id(alias)}",
                ))
            kb.row(tg_types.InlineKeyboardButton("➕ Добавить", callback_data="aso:add_acc"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_account_view(chat_id: int, acc: dict[str, Any], edit_msg_id: int | None = None):
            alias = acc.get("alias", "?")
            mf = acc.get("mafile", {})
            state = "❄ Заморожен" if acc.get("frozen") else ("⏳ Занят" if _account_in_use(alias) else "✅ Свободен")
            text = (
                "📝 *Редактирование аккаунта (оффлайн)*\n\n"
                f"👀 Состояние: *{state}*\n"
                f"👤 Логин: `{common.md_escape(acc.get('login', ''))}`\n"
                f"🔑 Пароль: `{common.md_escape(_mask(acc.get('password', '')))}`\n\n"
                "🗒 maFile данные:\n"
                f"• shared\\_secret: `{_mask(mf.get('shared_secret', ''))}`\n"
                f"• identity\\_secret: `{_mask(mf.get('identity_secret', ''))}`\n"
                f"• device\\_id: `{_mask(mf.get('device_id', ''))}`\n"
                f"• steam\\_id: `{common.md_escape(mf.get('steam_id', ''))}`\n"
            )
            sid = common.short_id(alias)
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "❄ Заморозить/Разморозить", callback_data=f"aso:freeze_acc:{sid}"))
            kb.row(tg_types.InlineKeyboardButton("🗑 Удалить аккаунт", callback_data=f"aso:del_acc:{sid}"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:accs"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_events(chat_id: int, edit_msg_id: int | None = None):
            evs = common.load_json(EVENTS_FILE, {})
            last = evs.get("last_notify_unclosed") or 0
            nxt = (last + 24 * 3600) if last else 0
            text = (
                "🚩 *Ивенты autosteamoffline*\n\n"
                "🔔 Уведомление незакрытых выдач:\n"
                f"• Последнее: {common.fmt_ts(last) if last else '—'}\n"
                f"• Следующее: {common.fmt_ts(nxt) if nxt else '—'}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_stats(chat_id: int, edit_msg_id: int | None = None):
            history = common.load_json(HISTORY_FILE, [])
            issued = sum(1 for h in history if h.get("event") == "issued")
            codes = sum(1 for h in history if h.get("event") == "guard_issued")
            no_acc = sum(1 for h in history if h.get("event") == "no_accounts")
            text = (
                "📊 *Статистика autosteamoffline*\n\n"
                "Статистика с момента запуска:\n"
                f"• Выполнено выдач: {issued}\n"
                f"• Steam Guard кодов выдано: {codes}\n"
                f"• Не нашлось аккаунта: {no_acc}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_instruction(chat_id: int, edit_msg_id: int | None = None):
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="aso:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, PLUGIN.instruction, kb, parse_mode="Markdown")

        def _resolve_acc(short: str) -> dict[str, Any] | None:
            for a in list_accounts():
                if common.short_id(a.get("alias", "")) == short:
                    return a
            return None


HANDLER = Handler()


# ─── Telegram utils ──────────────────────────────────────────────

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
        # На случай ошибок Markdown — фолбэк без форматирования.
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            LOGGER.debug("autosteamoffline: send_message fallback failed", exc_info=True)
