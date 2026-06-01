"""Общий helper-модуль для OAuth-доступа к IMAP-ящикам.

Используется плагинами, которым нужно читать письма (`authcode`, и т.д.)
без хранения пароля от ящика. Поддерживает три провайдера:

* `google` — Gmail (`oauth2.googleapis.com/token` → `imap.gmail.com:993`)
* `microsoft` — MS365 / Outlook / Hotmail / Live
* `yandex` — Yandex.Почта

Также содержит таблицу IMAP-хостов по домену, чтобы `authcode.py` мог
угадать `host:port` по email без явной настройки администратором.
"""
from __future__ import annotations

import imaplib
import logging
from typing import Any, Callable

import requests

LOGGER = logging.getLogger("playerok_bot._email_oauth")


# ── Таблица IMAP-хостов по домену (basic-auth fallback) ─────────────────
_HOST_MAP_IMAP: list[tuple[tuple[str, ...], tuple[str, int]]] = [
    (("@gmail.com", "@googlemail.com"), ("imap.gmail.com", 993)),
    (("@yahoo.com",), ("imap.mail.yahoo.com", 993)),
    (("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"),
     ("outlook.office365.com", 993)),
    (("@yandex.ru", "@yandex.com", "@ya.ru"), ("imap.yandex.com", 993)),
    (("@mail.ru", "@bk.ru", "@inbox.ru", "@list.ru", "@internet.ru"),
     ("imap.mail.ru", 993)),
    (("@icloud.com", "@me.com"), ("imap.mail.me.com", 993)),
    (("@aol.com",), ("imap.aol.com", 993)),
    (("@zoho.com",), ("imap.zoho.com", 993)),
    (("@protonmail.com", "@proton.me"), ("127.0.0.1", 1143)),  # bridge
]


def guess_imap_host(email_addr: str) -> tuple[str, int]:
    """Возвращает (host, port) для email — или ('', 993), если домен неизвестен."""
    addr = (email_addr or "").lower().strip()
    for suffixes, host_port in _HOST_MAP_IMAP:
        if any(addr.endswith(suf) for suf in suffixes):
            return host_port
    return ("", 993)


def guess_provider(email_addr: str) -> str | None:
    """Подсказка какого провайдера использовать для OAuth по домену."""
    addr = (email_addr or "").lower().strip()
    if addr.endswith(("@gmail.com", "@googlemail.com")):
        return "google"
    if addr.endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com")):
        return "microsoft"
    if addr.endswith(("@yandex.ru", "@yandex.com", "@ya.ru")):
        return "yandex"
    return None


# ── OAuth provider table ───────────────────────────────────────────────
OAUTH_PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "label": "Gmail",
        "token_url": "https://oauth2.googleapis.com/token",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "scope": "https://mail.google.com/",
        "client_secret_required": True,
    },
    "microsoft": {
        "label": "MS365 / Outlook",
        # `common` подходит для personal + work; для строго personal — `consumers`.
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "scope": "https://outlook.office365.com/IMAP.AccessAsUser.All offline_access",
        "client_secret_required": False,  # public/desktop клиент → без secret
    },
    "yandex": {
        "label": "Yandex",
        "token_url": "https://oauth.yandex.ru/token",
        "imap_host": "imap.yandex.com",
        "imap_port": 993,
        "scope": "mail:imap_full",
        "client_secret_required": True,
    },
}

PROVIDER_ALIASES: dict[str, str] = {
    "google": "google",
    "gmail": "google",
    "oauth_google": "google",
    "oauth_gmail": "google",
    "microsoft": "microsoft",
    "ms": "microsoft",
    "ms365": "microsoft",
    "outlook": "microsoft",
    "office365": "microsoft",
    "hotmail": "microsoft",
    "oauth_ms": "microsoft",
    "oauth_microsoft": "microsoft",
    "oauth_outlook": "microsoft",
    "yandex": "yandex",
    "ya": "yandex",
    "oauth_ya": "yandex",
    "oauth_yandex": "yandex",
}


def resolve_provider(name: str | None) -> str | None:
    """Принимает любой алиас провайдера и возвращает каноническое имя."""
    if not name:
        return None
    return PROVIDER_ALIASES.get(name.strip().lower())


def fetch_access_token(provider: str, client_id: str, client_secret: str,
                       refresh_token: str, timeout: float = 15.0) -> str | None:
    """Меняет refresh_token на access_token у выбранного провайдера.

    Возвращает access_token (str) или None при любой ошибке (с логированием).
    """
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        LOGGER.warning("Unknown OAuth provider: %s", provider)
        return None
    data: dict[str, str] = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    # Microsoft требует scope в refresh-запросе.
    if provider == "microsoft":
        data["scope"] = str(cfg["scope"])
    try:
        resp = requests.post(str(cfg["token_url"]), data=data, timeout=timeout)
    except Exception:
        LOGGER.exception("OAuth refresh failed (%s)", provider)
        return None
    if resp.status_code != 200:
        LOGGER.warning("OAuth refresh (%s) HTTP %s: %s",
                       provider, resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json().get("access_token")
    except Exception:
        LOGGER.exception("OAuth refresh (%s) bad JSON", provider)
        return None


def xoauth2_payload(email_addr: str, access_token: str) -> bytes:
    """Кодирует XOAUTH2 SASL-строку для IMAP authenticate()."""
    raw = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
    return raw.encode("utf-8")


def make_oauth_login(email_addr: str, access_token: str) -> Callable[[imaplib.IMAP4_SSL], Any]:
    """Возвращает функцию login(mail), подходящую для использования с IMAP_SSL."""
    def _login(mail: imaplib.IMAP4_SSL) -> Any:
        return mail.authenticate("XOAUTH2", lambda _: xoauth2_payload(email_addr, access_token))
    return _login


def open_imap(host: str, port: int, email_addr: str, *,
              auth_method: str = "imap_basic",
              password: str = "",
              provider: str | None = None,
              client_id: str = "",
              client_secret: str = "",
              refresh_token: str = "",
              timeout: float = 15.0) -> imaplib.IMAP4_SSL | None:
    """Универсальная фабрика IMAP-соединения.

    Возвращает залогиненный imaplib.IMAP4_SSL или None если не удалось.
    Caller отвечает за `logout()`.
    """
    if not host:
        return None
    try:
        mail = imaplib.IMAP4_SSL(host, int(port or 993), timeout=timeout)
    except Exception:
        LOGGER.exception("IMAP connect %s:%s failed", host, port)
        return None
    try:
        if auth_method == "imap_basic":
            mail.login(email_addr, password)
        elif auth_method == "oauth":
            prov = resolve_provider(provider)
            if not prov:
                LOGGER.warning("OAuth: неизвестный провайдер '%s'", provider)
                mail.logout()
                return None
            access = fetch_access_token(prov, client_id, client_secret, refresh_token)
            if not access:
                mail.logout()
                return None
            mail.authenticate("XOAUTH2",
                              lambda _: xoauth2_payload(email_addr, access))
        else:
            LOGGER.warning("Unknown auth_method '%s'", auth_method)
            mail.logout()
            return None
    except Exception:
        LOGGER.exception("IMAP login %s failed", email_addr)
        try:
            mail.logout()
        except Exception:
            pass
        return None
    return mail
