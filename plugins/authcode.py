"""Плагин выдачи mail-кода авторизации Steam (или любого другого сервиса)
через IMAP или OAuth (Gmail / MS365 / Yandex).

Покупатель пишет в чат Playerok команду `!mailcode` (или `!authcode`,
`!письмо`) — бот:

1. находит выдачу аккаунта в этом чате (через autosteamoffline и/или
   autosteamrental),
2. лезет в почтовый ящик аккаунта по IMAP или OAuth (или общий ящик из настроек),
3. парсит из последнего письма Steam код и отдаёт его покупателю.

Все настройки IMAP / OAuth — в `storage/plugins/authcode/config.json` либо в
карточке аккаунта `accounts.json`:

* `email_imap_host`, `email_imap_port`, `email_imap_login`, `email_imap_password` —
  классический IMAP с паролем (или app password).
* `email_auth_method = "oauth"` + `email_oauth_provider` + `email_oauth_client_id` +
  `email_oauth_client_secret` + `email_oauth_refresh_token` — OAuth XOAUTH2.
  Поддерживаемые провайдеры: `google`, `microsoft`, `yandex`.

Поддерживает фильтр отправителей (по умолчанию `noreply@steampowered.com`),
регулярку для извлечения кода (по умолчанию `[A-Z0-9]{5}`),
rate-limit по покупателю и anti-abuse алерты админу.
"""
from __future__ import annotations

import imaplib
import logging
import os
import re
import threading
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Any

from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _email_oauth
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.authcode")
STORAGE_DIR = os.path.join("storage", "plugins", "authcode")
ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")

DEFAULT_CONFIG: dict[str, Any] = {
    # «Общий» IMAP — если у покупателя нет личного, используется этот.
    # Поля можно оставить пустыми — тогда работает только per-account.
    "imap_host": "",
    "imap_port": 993,
    "imap_login": "",
    "imap_password": "",
    # Способ аутентификации «общего» ящика: imap_basic | oauth.
    "auth_method": "imap_basic",
    # OAuth-параметры «общего» ящика (используются при auth_method == "oauth")
    "oauth_provider": "",          # google | microsoft | yandex
    "oauth_client_id": "",
    "oauth_client_secret": "",       # для Microsoft public client можно оставить пустым
    "oauth_refresh_token": "",
    # Фильтр писем
    "subject_contains": "Steam",
    "sender_contains": "noreply@steampowered.com",
    "look_back_minutes": 30,         # искать только письма не старше N минут
    "max_messages_scan": 20,         # просматриваем последние N писем
    # Регулярка для извлечения кода. По умолчанию — 5-символьный код Steam
    # (буквы/цифры в верхнем регистре).
    "code_regex": r"\b[A-Z0-9]{5}\b",
    # Rate-limit и anti-abuse
    "rate_limit_sec": 10,                # мин. интервал между запросами одного покупателя, сек
    "abuse_per_hour": 0,                 # 0 = выкл. Иначе лимит запросов/час на покупателя
    "abuse_action": "warn_admin",       # warn_admin | block | warn_admin_block
    # Шаблоны сообщений в Playerok
    "templates": {
        "code": (
            "📧 Код подтверждения из письма:\n"
            "{code}\n\n"
            "Письмо отправлено {sent_ago} назад."
        ),
        "no_code": (
            "📭 Не нашёл свежего письма с кодом за последние {look_back} мин. "
            "Попробуйте инициировать вход в Steam ещё раз и через 30 секунд "
            "повторите команду."
        ),
        "no_config": (
            "⚙️ Для этого аккаунта не настроен доступ к почте. "
            "Обратитесь к продавцу командой !продавец."
        ),
        "error": (
            "❌ Ошибка получения письма ({reason}). "
            "Попробуйте позже или напишите !продавец."
        ),
        "no_assignment": (
            "ℹ️ Я не вижу активной выдачи аккаунта в этом чате. "
            "Команда !mailcode работает только после оплаты."
        ),
        "rate_limited": (
            "⏳ Слишком часто. Повтори через {wait_sec} сек."
        ),
    },
    "allow_without_assignment": False,
}


ABUSE_ACTIONS = ("warn_admin", "block", "warn_admin_block")
_ABUSE_ALERT_COOLDOWN_SEC = 30 * 60


class _Runtime:
    """Память в RAM для rate-limit и anti-abuse. Не персистится."""
    last_request: dict[str, float] = {}     # buyer_key -> ts
    hourly: dict[str, list[float]] = {}     # buyer_key -> [ts...]
    abuse_alerted: dict[str, float] = {}    # buyer_key -> ts последнего алерта
    lock = threading.Lock()


def _abuse_record_request(key: str) -> int:
    """Записывает запрос и возвращает число запросов за последние 60 мин."""
    now_ts = time.time()
    with _Runtime.lock:
        bucket = _Runtime.hourly.setdefault(key, [])
        cutoff = now_ts - 3600.0
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        bucket.append(now_ts)
        return len(bucket)


def _abuse_should_alert(key: str) -> bool:
    """True если по key ещё не было алерта в течении _ABUSE_ALERT_COOLDOWN_SEC."""
    now_ts = time.time()
    with _Runtime.lock:
        last = _Runtime.abuse_alerted.get(key, 0.0)
        if now_ts - last < _ABUSE_ALERT_COOLDOWN_SEC:
            return False
        _Runtime.abuse_alerted[key] = now_ts
        return True


def _rate_limit_remaining(key: str, limit_sec: int) -> int:
    """Сколько секунд осталось ждать (0 = можно сразу). Обновляет ts."""
    if limit_sec <= 0:
        return 0
    now_ts = time.time()
    with _Runtime.lock:
        last = _Runtime.last_request.get(key, 0.0)
        wait = int(last + limit_sec - now_ts)
        if wait > 0:
            return wait
        _Runtime.last_request[key] = now_ts
        return 0


def get_config() -> dict[str, Any]:
    common.ensure_dir(STORAGE_DIR)
    cfg = common.load_json(CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        cfg = {}
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v if not isinstance(v, dict) else dict(v)
    if "templates" in cfg:
        for tk, tv in DEFAULT_CONFIG["templates"].items():
            cfg["templates"].setdefault(tk, tv)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    common.save_json(CONFIG_FILE, cfg)


def render_template(name: str, **kwargs: Any) -> str:
    cfg = get_config()
    tpl = cfg["templates"].get(name) or DEFAULT_CONFIG["templates"].get(name, "")
    return common.fmt_template(tpl, **kwargs)


def list_accounts() -> list[dict[str, Any]]:
    common.ensure_dir(STORAGE_DIR)
    return common.load_json(ACCOUNTS_FILE, [])


def save_accounts(accs: list[dict[str, Any]]) -> None:
    common.ensure_dir(STORAGE_DIR)
    common.save_json(ACCOUNTS_FILE, accs)


def find_account(alias: str) -> dict[str, Any] | None:
    for a in list_accounts():
        if a.get("alias", "").lower() == alias.lower():
            return a
    return None


def upsert_account(acc: dict[str, Any]) -> None:
    accs = list_accounts()
    for i, a in enumerate(accs):
        if a.get("alias", "").lower() == acc["alias"].lower():
            accs[i] = acc
            break
    else:
        accs.append(acc)
    save_accounts(accs)


def delete_account(alias: str) -> bool:
    accs = list_accounts()
    new = [a for a in accs if a.get("alias", "").lower() != alias.lower()]
    if len(new) == len(accs):
        return False
    save_accounts(new)
    return True


def log_event(event: str, **extra: Any) -> None:
    common.ensure_dir(STORAGE_DIR)
    entry = {"ts": common.now(), "event": event}
    entry.update({k: v for k, v in extra.items() if v is not None})
    history = common.load_json(HISTORY_FILE, [])
    history.append(entry)
    if len(history) > 5000:
        history = history[-5000:]
    common.save_json(HISTORY_FILE, history)


# ─── IMAP ─────────────────────────────────────────────────────────

def _decode(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        for enc in ("utf-8", "cp1251", "latin1"):
            try:
                return s.decode(enc)
            except UnicodeDecodeError:
                continue
        return s.decode("utf-8", errors="replace")
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


def _extract_body(msg) -> str:
    """Достаёт plain-text тело письма (HTML — резерв)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return _decode(payload)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                return _decode(payload)
        return ""
    return _decode(msg.get_payload(decode=True) or b"")


def fetch_latest_code(*, host: str, port: int, login: str, password: str = "",
                     auth_method: str = "imap_basic",
                     oauth_provider: str | None = None,
                     oauth_client_id: str = "",
                     oauth_client_secret: str = "",
                     oauth_refresh_token: str = "",
                     subject_contains: str = "",
                     sender_contains: str = "",
                     code_regex: str = r"\b[A-Z0-9]{5}\b",
                     look_back_minutes: int = 30,
                     max_scan: int = 20,
                     ) -> dict[str, Any] | None:
    """Логинится в IMAP (паролем или OAuth), ищет последнее письмо со steam-кодом.

    Возвращает {"code": ..., "sent_ts": int} или None если не найдено.
    Бросает Exception на сетевые ошибки.
    """
    if not (host and login):
        return None
    if auth_method == "imap_basic" and not password:
        return None
    if auth_method == "oauth" and not (oauth_client_id and oauth_refresh_token):
        return None
    pat = re.compile(code_regex)
    cutoff = int(time.time()) - look_back_minutes * 60

    imap = _email_oauth.open_imap(
        host=host,
        port=int(port or 993),
        email_addr=login,
        auth_method=auth_method,
        password=password,
        provider=oauth_provider,
        client_id=oauth_client_id,
        client_secret=oauth_client_secret,
        refresh_token=oauth_refresh_token,
    )
    if imap is None:
        return None
    try:
        imap.select("INBOX")
        typ, data = imap.search(None, "ALL")
        if typ != "OK":
            return None
        ids = data[0].split()
        if not ids:
            return None
        # Берём последние N писем (свежие в конце)
        for num in reversed(ids[-max_scan:]):
            typ, raw = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = message_from_bytes(raw[0][1])
            subject = _decode(msg.get("Subject"))
            from_ = _decode(msg.get("From"))
            if subject_contains and subject_contains.lower() not in subject.lower():
                continue
            if sender_contains and sender_contains.lower() not in from_.lower():
                continue
            # Время письма
            from email.utils import parsedate_to_datetime
            try:
                sent_dt = parsedate_to_datetime(msg.get("Date"))
                sent_ts = int(sent_dt.timestamp())
            except Exception:
                sent_ts = 0
            if sent_ts and sent_ts < cutoff:
                continue
            body = _extract_body(msg)
            haystack = f"{subject}\n{body}"
            m = pat.search(haystack)
            if m:
                code = m.group(0)
                return {"code": code, "sent_ts": sent_ts,
                        "subject": subject, "from": from_}
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def build_fetch_kwargs(cfg: dict[str, Any], acc: dict[str, Any] | None) -> dict[str, Any]:
    """Собирает вход для fetch_latest_code: per-account переопределяет общие настройки.

    Автоматически дополняет host:port по домену email, если они не заданы явно.
    """
    acc = acc or {}
    auth_method = (acc.get("email_auth_method")
                   or cfg.get("auth_method", "imap_basic")).lower()
    login = acc.get("email_imap_login") or cfg.get("imap_login", "")
    password = acc.get("email_imap_password") or cfg.get("imap_password", "")
    provider = (acc.get("email_oauth_provider")
                or cfg.get("oauth_provider") or None)
    if auth_method == "oauth" and not provider:
        provider = _email_oauth.guess_provider(login)
    host = acc.get("email_imap_host") or cfg.get("imap_host", "")
    port = acc.get("email_imap_port") or cfg.get("imap_port", 993)
    if not host:
        guess_host, guess_port = _email_oauth.guess_imap_host(login)
        if guess_host:
            host = guess_host
            port = guess_port
    return {
        "host": host,
        "port": int(port or 993),
        "login": login,
        "password": password,
        "auth_method": auth_method,
        "oauth_provider": provider,
        "oauth_client_id": (acc.get("email_oauth_client_id")
                            or cfg.get("oauth_client_id", "")),
        "oauth_client_secret": (acc.get("email_oauth_client_secret")
                                or cfg.get("oauth_client_secret", "")),
        "oauth_refresh_token": (acc.get("email_oauth_refresh_token")
                                or cfg.get("oauth_refresh_token", "")),
        "subject_contains": cfg.get("subject_contains", ""),
        "sender_contains": cfg.get("sender_contains", ""),
        "code_regex": cfg.get("code_regex") or r"\b[A-Z0-9]{5}\b",
        "look_back_minutes": int(cfg.get("look_back_minutes", 30)),
        "max_scan": int(cfg.get("max_messages_scan", 20)),
    }


def _credentials_ok(kwargs: dict[str, Any]) -> bool:
    """Быстрая проверка: хватает ли полей для выбранного auth_method."""
    if not (kwargs.get("host") and kwargs.get("login")):
        return False
    am = kwargs.get("auth_method")
    if am == "oauth":
        prov = _email_oauth.resolve_provider(kwargs.get("oauth_provider"))
        if not prov:
            return False
        if not kwargs.get("oauth_client_id") or not kwargs.get("oauth_refresh_token"):
            return False
        prov_cfg = _email_oauth.OAUTH_PROVIDERS[prov]
        if prov_cfg["client_secret_required"] and not kwargs.get("oauth_client_secret"):
            return False
        return True
    # imap_basic
    return bool(kwargs.get("password"))


# ─── Метаданные плагина ──────────────────────────────────────────

PLUGIN = Plugin(
    id="authcode",
    name="authcode",
    icon="📧",
    description=(
        "Выдача mail-кода (Steam authcode) из почтового ящика по IMAP "
        "по команде покупателя в чате Playerok. /authcode в Telegram-боте."
    ),
    instruction=(
        "🔌 *Плагин authcode*\n\n"
        "Логика:\n"
        "1. Покупатель пишет в чате Playerok: `!mailcode` (алиасы: `!authcode`, `!письмо`).\n"
        "2. Бот идёт в IMAP-ящик (общий или привязанный к аккаунту), ищет "
        "последнее письмо от Steam за последние N минут и парсит из него "
        "5-символьный код.\n"
        "3. Шлёт код в чат.\n\n"
        "*Где задать IMAP:*\n"
        "• Общий ящик: меню → Настройки → IMAP-хост / порт / логин / пароль.\n"
        "• Per-account: в карточке аккаунта (Аккаунты → выбрать → "
        "✉️ Настроить почту).\n\n"
        "По умолчанию плагин требует, чтобы в текущем чате была активная "
        "выдача от autosteamoffline или autosteamrental — иначе откажет."
    ),
    default_enabled=False,
)


# ─── Handler ──────────────────────────────────────────────────────

class Handler:
    def setup(self, ctx: PluginContext) -> None:
        common.ensure_dir(STORAGE_DIR)
        get_config()

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes
        et = getattr(event, "type", None)
        if et is EventTypes.NEW_MESSAGE:
            return self._handle_new_message(event, ctx)
        return False

    def _handle_new_message(self, event: Any, ctx: PluginContext) -> bool:
        msg = getattr(event, "message", None)
        if msg is None:
            return False
        user = getattr(msg, "user", None)
        if user is None or not ctx.playerok_acc:
            return False
        if getattr(user, "id", None) == getattr(ctx.playerok_acc, "id", None):
            return False
        text = (getattr(msg, "text", "") or "").strip().lower()
        chat = getattr(event, "chat", None)
        chat_id = getattr(chat, "id", None) if chat else None
        if not chat_id:
            return False
        if not any(text.startswith(cmd) for cmd in
                   ("!mailcode", "!authcode", "!письмо", "!email", "!почта")):
            return False

        cfg = get_config()
        # Находим активную выдачу — алиас аккаунта для этого чата.
        alias = self._find_alias_for_chat(str(chat_id))
        if not alias and not cfg.get("allow_without_assignment"):
            self._send(ctx, chat_id, render_template("no_assignment"))
            return True

        acc = find_account(alias) if alias else None

        # Ключ buyer_key для rate-limit/abuse — предпочитаю Playerok user.id, fallback chat_id.
        buyer_id = getattr(user, "id", None)
        buyer_username = getattr(user, "username", None)
        buyer_key = f"u:{buyer_id}" if buyer_id else f"c:{chat_id}"

        # Rate-limit per buyer.
        rl_sec = int(cfg.get("rate_limit_sec", 0) or 0)
        wait = _rate_limit_remaining(buyer_key, rl_sec)
        if wait > 0:
            self._send(ctx, chat_id, render_template("rate_limited", wait_sec=wait))
            log_event("rate_limited", chat_id=chat_id, alias=alias,
                      buyer=str(buyer_id or buyer_username or ""), wait_sec=wait)
            return True

        # Anti-abuse скользящее окно 60 мин на покупателя.
        abuse_limit = int(cfg.get("abuse_per_hour", 0) or 0)
        if abuse_limit > 0:
            count = _abuse_record_request(buyer_key)
            if count > abuse_limit:
                action = str(cfg.get("abuse_action", "warn_admin")).lower()
                if action not in ABUSE_ACTIONS:
                    action = "warn_admin"
                blocked = action in ("block", "warn_admin_block")
                should_alert = action in ("warn_admin", "warn_admin_block")
                if should_alert and _abuse_should_alert(buyer_key):
                    self._notify_admin(
                        ctx,
                        "\U0001f6a8 <b>Anti-abuse:</b> authcode\n"
                        f"chat_id: <code>{chat_id}</code>\n"
                        f"buyer: <code>{buyer_id or '?'}</code> "
                        f"({buyer_username or '?'})\n"
                        f"\u0430\u043b\u0438\u0430\u0441: <code>{alias or '\u2014'}</code>\n"
                        f"\u043a\u043e\u043c\u0430\u043d\u0434\u0430: <code>{text[:80]}</code>\n"
                        f"\u0437\u0430 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0447\u0430\u0441: <b>{count}</b> "
                        f"(\u043b\u0438\u043c\u0438\u0442 {abuse_limit})\n"
                        f"action: <code>{action}</code>",
                    )
                log_event("abuse_hit", chat_id=chat_id, alias=alias,
                          buyer=str(buyer_id or buyer_username or ""),
                          count=count, limit=abuse_limit, action=action)
                if blocked:
                    return True

        # Собираем эффективные параметры (per-account override global, host auto-guess).
        fkw = build_fetch_kwargs(cfg, acc)
        if not _credentials_ok(fkw):
            self._send(ctx, chat_id, render_template("no_config"))
            log_event("no_config", chat_id=chat_id, alias=alias,
                      auth_method=fkw.get("auth_method"))
            return True

        try:
            result = fetch_latest_code(**fkw)
        except Exception as exc:
            LOGGER.error("IMAP fetch failed", exc_info=True)
            self._send(ctx, chat_id, render_template("error", reason=str(exc)))
            log_event("imap_error", chat_id=chat_id, alias=alias,
                      error=str(exc))
            return True

        if not result:
            self._send(ctx, chat_id, render_template(
                "no_code", look_back=cfg.get("look_back_minutes", 30)))
            log_event("no_code", chat_id=chat_id, alias=alias)
            return True

        sent_ts = int(result.get("sent_ts") or 0)
        sent_ago = "только что" if not sent_ts else common.human_seconds(
            max(0, common.now() - sent_ts))
        self._send(ctx, chat_id, render_template(
            "code", code=result["code"], sent_ago=sent_ago,
        ))
        log_event("code_sent", chat_id=chat_id, alias=alias,
                  code=result["code"], subject=result.get("subject"))
        return True

    @staticmethod
    def _find_alias_for_chat(chat_id: str) -> str | None:
        """Ищет активную выдачу в autosteamrental, потом в autosteamoffline."""
        try:
            from . import autosteamrental as asr
            a = asr.find_assignment_by_chat(chat_id)
            if a:
                return a.get("alias")
        except Exception:
            pass
        try:
            from . import autosteamoffline as aso
            a = aso.find_assignment_by_chat(chat_id)
            if a:
                return a.get("alias")
        except Exception:
            pass
        return None

    @staticmethod
    def _send(ctx: PluginContext, chat_id: str, text: str) -> bool:
        if not ctx.playerok_acc:
            return False
        try:
            ctx.playerok_acc.send_message(chat_id=chat_id, text=text)
            return True
        except Exception:
            LOGGER.exception("authcode: send_message failed")
            return False

    @staticmethod
    def _notify_admin(ctx: PluginContext, html_text: str) -> None:
        """Шлёт admin_id HTML-алерт. Молча проглатывает ошибки TG."""
        bot = getattr(ctx, "bot", None)
        admin_id = getattr(ctx, "admin_id", None)
        if not (bot and admin_id):
            return
        try:
            bot.send_message(admin_id, html_text, parse_mode="HTML")
        except Exception:
            LOGGER.exception("authcode: admin notify failed")

    # ── Telegram UI ───────────────────────────────────────────────────────

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id

        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["authcode"])
        def cmd_authcode(message):
            if message.from_user.id != admin_id:
                return
            send_main(message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("auc:"))
        def on_callback(call):
            if call.from_user.id != admin_id:
                return
            data = call.data
            chat_id = call.message.chat.id
            msg_id = call.message.message_id
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action == "main":
                send_main(chat_id, msg_id)
            elif action == "settings":
                send_settings(chat_id, msg_id)
            elif action == "oauth":
                send_oauth_settings(chat_id, msg_id)
            elif action == "abuse":
                send_abuse_settings(chat_id, msg_id)
            elif action == "setam":
                value = parts[2] if len(parts) > 2 else ""
                cfg = get_config()
                if value in ("imap_basic", "oauth"):
                    cfg["auth_method"] = value
                    save_config(cfg)
                send_oauth_settings(chat_id, msg_id)
            elif action == "setprov":
                value = parts[2] if len(parts) > 2 else ""
                cfg = get_config()
                resolved = _email_oauth.resolve_provider(value)
                cfg["oauth_provider"] = resolved or ""
                save_config(cfg)
                send_oauth_settings(chat_id, msg_id)
            elif action == "setab":
                value = parts[2] if len(parts) > 2 else ""
                cfg = get_config()
                if value in ABUSE_ACTIONS:
                    cfg["abuse_action"] = value
                    save_config(cfg)
                send_abuse_settings(chat_id, msg_id)
            elif action == "edit":
                # auc:edit:<field>
                field = parts[2] if len(parts) > 2 else ""
                if field:
                    wait_state[chat_id] = {"step": "edit_setting", "field": field}
                    bot.send_message(
                        chat_id,
                        f"✏️ Введи новое значение для `{field}`. "
                        f"Отмена: /cancel",
                        parse_mode="Markdown",
                    )
            elif action == "test":
                send_test(chat_id, msg_id)
            elif action == "events":
                send_events(chat_id, msg_id)
            elif action == "instr":
                send_instr(chat_id, msg_id)

        @bot.message_handler(commands=["cancel"])
        def cancel(message):
            if message.from_user.id != admin_id:
                return
            if message.chat.id in wait_state:
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id, "❎ Отменено.")

        @bot.message_handler(func=lambda m: m.from_user.id == admin_id
                             and m.chat.id in wait_state)
        def on_text(message):
            state = wait_state.get(message.chat.id) or {}
            if state.get("step") == "edit_setting":
                field = state["field"]
                cfg = get_config()
                int_fields = ("imap_port", "look_back_minutes",
                              "max_messages_scan", "rate_limit_sec",
                              "abuse_per_hour")
                if field in int_fields:
                    try:
                        cfg[field] = int((message.text or "").strip())
                    except ValueError:
                        bot.send_message(message.chat.id, "❌ Не число.")
                        return
                else:
                    cfg[field] = (message.text or "").strip()
                save_config(cfg)
                wait_state.pop(message.chat.id, None)
                bot.send_message(message.chat.id,
                                 f"✅ `{field}` обновлён.",
                                 parse_mode="Markdown")
                if field.startswith("oauth_") or field == "auth_method":
                    send_oauth_settings(message.chat.id)
                elif field.startswith("abuse_") or field == "rate_limit_sec":
                    send_abuse_settings(message.chat.id)
                else:
                    send_settings(message.chat.id)

        # ── Renderers ─────────────────────────────────────────────────────

        def send_main(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            am = cfg.get("auth_method", "imap_basic")
            if am == "oauth":
                prov = _email_oauth.resolve_provider(cfg.get("oauth_provider"))
                if prov:
                    method_line = (f"🔑 OAuth: "
                                   f"{_email_oauth.OAUTH_PROVIDERS[prov]['label']}")
                else:
                    method_line = "🔑 OAuth: — (выбери провайдера)"
            else:
                host = cfg.get("imap_host") or "—"
                method_line = f"📮 IMAP: `{host}`"
            text = (
                "📧 *Меню authcode*\n\n"
                "_Выдача mail-кода из почтового ящика по команде покупателя._\n\n"
                f"{method_line}\n"
                f"⏱ Look-back: {cfg.get('look_back_minutes')} мин\n"
                f"🔎 Регулярка: `{cfg.get('code_regex')}`"
            )
            kb = tg_types.InlineKeyboardMarkup(row_width=2)
            kb.row(
                tg_types.InlineKeyboardButton("⚙️ IMAP", callback_data="auc:settings"),
                tg_types.InlineKeyboardButton("🔑 OAuth", callback_data="auc:oauth"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("🛡 Anti-abuse", callback_data="auc:abuse"),
                tg_types.InlineKeyboardButton("🧪 Тест", callback_data="auc:test"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("📒 Ивенты", callback_data="auc:events"),
                tg_types.InlineKeyboardButton("📖 Инструкция", callback_data="auc:instr"),
            )
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_settings(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            text = (
                "⚙️ *Настройки IMAP*\n\n"
                f"• host: `{cfg.get('imap_host') or '—'}`\n"
                f"• port: `{cfg.get('imap_port')}`\n"
                f"• login: `{_mask(cfg.get('imap_login') or '')}`\n"
                f"• password: `{'***' if cfg.get('imap_password') else '—'}`\n"
                f"• subject contains: `{cfg.get('subject_contains') or '—'}`\n"
                f"• sender contains: `{cfg.get('sender_contains') or '—'}`\n"
                f"• code regex: `{cfg.get('code_regex')}`\n"
                f"• look back: `{cfg.get('look_back_minutes')}` мин\n"
                f"• max scan: `{cfg.get('max_messages_scan')}`"
            )
            kb = tg_types.InlineKeyboardMarkup(row_width=2)
            for field, label in [
                ("imap_host", "host"), ("imap_port", "port"),
                ("imap_login", "login"), ("imap_password", "password"),
                ("subject_contains", "тема"), ("sender_contains", "отправитель"),
                ("code_regex", "regex"), ("look_back_minutes", "look-back"),
            ]:
                kb.add(tg_types.InlineKeyboardButton(
                    f"✏️ {label}", callback_data=f"auc:edit:{field}"))
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_oauth_settings(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            am = cfg.get("auth_method", "imap_basic")
            prov_key = _email_oauth.resolve_provider(cfg.get("oauth_provider"))
            prov_label = (_email_oauth.OAUTH_PROVIDERS[prov_key]["label"]
                          if prov_key else "—")
            text = (
                "🔑 *OAuth-доступ к почте*\n\n"
                f"Способ: `{am}`\n"
                f"Провайдер: `{prov_label}`\n"
                f"client\\_id: `{_mask(cfg.get('oauth_client_id') or '')}`\n"
                f"client\\_secret: `{'***' if cfg.get('oauth_client_secret') else '—'}`\n"
                f"refresh\\_token: `{'***' if cfg.get('oauth_refresh_token') else '—'}`\n\n"
                "_Для Microsoft public-app «client\\_secret» можно оставить пустым._"
            )
            kb = tg_types.InlineKeyboardMarkup(row_width=2)
            kb.row(
                tg_types.InlineKeyboardButton(
                    ("☑️" if am == "imap_basic" else "◻️") + " IMAP-пароль",
                    callback_data="auc:setam:imap_basic"),
                tg_types.InlineKeyboardButton(
                    ("☑️" if am == "oauth" else "◻️") + " OAuth",
                    callback_data="auc:setam:oauth"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    ("☑️" if prov_key == "google" else "◻️") + " Gmail",
                    callback_data="auc:setprov:google"),
                tg_types.InlineKeyboardButton(
                    ("☑️" if prov_key == "microsoft" else "◻️") + " MS365",
                    callback_data="auc:setprov:microsoft"),
                tg_types.InlineKeyboardButton(
                    ("☑️" if prov_key == "yandex" else "◻️") + " Yandex",
                    callback_data="auc:setprov:yandex"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("✏️ client_id",
                                              callback_data="auc:edit:oauth_client_id"),
                tg_types.InlineKeyboardButton("✏️ client_secret",
                                              callback_data="auc:edit:oauth_client_secret"),
            )
            kb.row(
                tg_types.InlineKeyboardButton("✏️ refresh_token",
                                              callback_data="auc:edit:oauth_refresh_token"),
                tg_types.InlineKeyboardButton("✏️ login (email)",
                                              callback_data="auc:edit:imap_login"),
            )
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_abuse_settings(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            action = cfg.get("abuse_action", "warn_admin")
            enabled = "вкл" if int(cfg.get("abuse_per_hour", 0) or 0) else "выкл"
            text = (
                "🛡 *Rate-limit и anti-abuse*\n\n"
                f"• rate\\_limit\\_sec: `{cfg.get('rate_limit_sec')}`\n"
                f"• abuse\\_per\\_hour: `{cfg.get('abuse_per_hour')}` ({enabled})\n"
                f"• abuse\\_action: `{action}`\n\n"
                "_Счётчик «за 1 час» по покупателю. При превышении —"
                " warn\\_admin / block / warn\\_admin\\_block._"
            )
            kb = tg_types.InlineKeyboardMarkup(row_width=2)
            kb.row(
                tg_types.InlineKeyboardButton("✏️ rate_limit_sec",
                                              callback_data="auc:edit:rate_limit_sec"),
                tg_types.InlineKeyboardButton("✏️ abuse_per_hour",
                                              callback_data="auc:edit:abuse_per_hour"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    ("☑️" if action == "warn_admin" else "◻️") + " warn_admin",
                    callback_data="auc:setab:warn_admin"),
                tg_types.InlineKeyboardButton(
                    ("☑️" if action == "block" else "◻️") + " block",
                    callback_data="auc:setab:block"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    ("☑️" if action == "warn_admin_block" else "◻️") + " warn+block",
                    callback_data="auc:setab:warn_admin_block"),
            )
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def send_test(chat_id: int, edit_msg_id: int | None = None):
            cfg = get_config()
            fkw = build_fetch_kwargs(cfg, None)
            if not _credentials_ok(fkw):
                if not (fkw["host"] and fkw["login"]):
                    missing = "• нет host/login"
                elif fkw["auth_method"] == "oauth":
                    missing = ("• OAuth: не хватает provider/client_id/"
                               "refresh_token (или client_secret для Gmail/Yandex)")
                else:
                    missing = "• IMAP: не введён пароль"
                text = f"⚠️ Недостаточно данных для теста:\n{missing}"
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
                _send_or_edit(bot, chat_id, edit_msg_id, text, kb,
                              parse_mode="Markdown")
                return
            try:
                result = fetch_latest_code(**fkw)
            except Exception as exc:
                text = f"❌ IMAP-ошибка: `{common.md_escape(str(exc))}`"
                kb = tg_types.InlineKeyboardMarkup()
                kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
                _send_or_edit(bot, chat_id, edit_msg_id, text, kb,
                              parse_mode="Markdown")
                return
            if not result:
                text = ("📭 Подходящих писем не нашёл. "
                        "Проверь фильтры темы/отправителя и look-back.")
            else:
                text = (
                    "✅ Найдено письмо:\n"
                    f"• От: `{common.md_escape(result.get('from', ''))}`\n"
                    f"• Тема: `{common.md_escape(result.get('subject', ''))}`\n"
                    f"• Код: `{result['code']}`"
                )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb,
                          parse_mode="Markdown")

        def send_events(chat_id: int, edit_msg_id: int | None = None):
            history = common.load_json(HISTORY_FILE, [])
            recent = history[-20:]
            if not recent:
                text = "📒 Пока пусто."
            else:
                lines = ["📒 *Последние события authcode:*\n"]
                for h in reversed(recent):
                    lines.append(f"• `{common.fmt_ts(h['ts'])}` {h['event']}")
                text = "\n".join(lines)
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, text, kb,
                          parse_mode="Markdown")

        def send_instr(chat_id: int, edit_msg_id: int | None = None):
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton("‹ Назад", callback_data="auc:main"))
            _send_or_edit(bot, chat_id, edit_msg_id, PLUGIN.instruction, kb,
                          parse_mode="Markdown")


def _mask(s: str) -> str:
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "***" + s[-1]


def _send_or_edit(bot, chat_id: int, msg_id: int | None, text: str,
                  kb=None, parse_mode: str | None = None) -> None:
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id,
                                  parse_mode=parse_mode, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=kb)


HANDLER = Handler()
