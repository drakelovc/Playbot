"""Плагин chat_manager -- просмотр чатов, TG-bridge, quick replies и перевод.

Позволяет:
  * Просматривать список недавних чатов.
  * Отправлять текстовые ответы и изображения через Playerok API.
  * **TG-bridge**: каждый Playerok-чат = 1 топик в TG-supergroup
    (включёны форум-топики). Отвечаешь прямо из топика
    — бот пересылает в Playerok.
  * **Quick replies** — шаблоны в карточке топика.
  * **Перевод** входящих/исходящих через LibreTranslate /
    Google / DeepL.
  * Уведомления о новых сообщениях (старый режим).

Telegram-команда -- /chat.
Хранилище: storage/plugins/chat_manager/config.json,
           storage/plugins/chat_manager/bridges.json.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests
from telebot import types as tg_types

from . import Plugin, PluginContext
from . import _steam_common as common

LOGGER = logging.getLogger("playerok_bot.chat_manager")
STORAGE_DIR = os.path.join("storage", "plugins", "chat_manager")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
BRIDGE_FILE = os.path.join(STORAGE_DIR, "bridges.json")
TRANSLATE_CACHE_FILE = os.path.join(STORAGE_DIR, "translate_cache.json")

# Кеш переводов (простой LRU в RAM) — чтобы не дёргать API на однинаковых фразах.
_TRANSLATE_CACHE: dict[tuple[str, str, str], str] = {}
_TRANSLATE_LOCK = threading.Lock()
_TRANSLATE_CACHE_MAX = 256
_TRANSLATE_CACHE_LOADED = False
_TRANSLATE_DIRTY = False
_TRANSLATE_SAVE_INTERVAL = 30.0  # сек, debounce между сохранениями
_TRANSLATE_LAST_SAVE = 0.0


def _cache_key_to_str(key: tuple[str, str, str]) -> str:
    """Сериализуем (provider, target, text) в одну строку для JSON."""
    provider, target, text = key
    return f"{provider}\u241f{target}\u241f{text}"


def _cache_key_from_str(raw: str) -> tuple[str, str, str] | None:
    parts = raw.split("\u241f", 2)
    if len(parts) != 3:
        return None
    return (parts[0], parts[1], parts[2])


def _load_translate_cache() -> None:
    """Загружает кэш переводов из JSON при первом обращении."""
    global _TRANSLATE_CACHE_LOADED
    if _TRANSLATE_CACHE_LOADED:
        return
    _TRANSLATE_CACHE_LOADED = True
    try:
        data = common.load_json(TRANSLATE_CACHE_FILE, {}) or {}
    except Exception:
        LOGGER.exception("translate cache load failed")
        return
    if not isinstance(data, dict):
        return
    loaded = 0
    for raw_key, translated in data.items():
        key = _cache_key_from_str(str(raw_key))
        if key is None or not isinstance(translated, str):
            continue
        _TRANSLATE_CACHE[key] = translated
        loaded += 1
        if loaded >= _TRANSLATE_CACHE_MAX:
            break
    LOGGER.debug("translate cache: loaded %d entries", loaded)


def _save_translate_cache(force: bool = False) -> None:
    """Сохраняет кэш переводов в JSON. Debounce по _TRANSLATE_SAVE_INTERVAL."""
    global _TRANSLATE_DIRTY, _TRANSLATE_LAST_SAVE
    now_ts = time.time()
    with _TRANSLATE_LOCK:
        if not _TRANSLATE_DIRTY and not force:
            return
        if not force and (now_ts - _TRANSLATE_LAST_SAVE) < _TRANSLATE_SAVE_INTERVAL:
            return
        snapshot = {_cache_key_to_str(k): v
                    for k, v in _TRANSLATE_CACHE.items()}
        _TRANSLATE_DIRTY = False
        _TRANSLATE_LAST_SAVE = now_ts
    try:
        common.save_json(TRANSLATE_CACHE_FILE, snapshot)
    except Exception:
        LOGGER.exception("translate cache save failed")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "notify_new_messages": True,
    "show_image_button": True,
    # ─── TG-bridge ───
    "bridge_enabled": False,
    # ID supergroup'ы с включёнными форум-топиками (отрицательный числовой ID).
    "bridge_chat_id": 0,
    # Префикс к имени топика (аккаунт Playerok), если multi-account.
    "bridge_topic_prefix": "",
    # Закрывать топик, когда чат Playerok «завершён».
    "bridge_close_finished": True,
    # Отправлять в топик и свои исходящие (дабл-loop подавлён).
    "bridge_echo_own": False,
    # Не создавать топики для чатов без входящих сообщений
    # (напр. служебных от самого Playerok). True = только покупательские.
    "bridge_only_buyer_messages": True,
    # Количество quick-reply кнопок на ряд.
    "bridge_quick_reply_columns": 2,

    # ─── Quick replies ───
    # Список шаблонов: [{"label": "...", "text": "..."}].
    "quick_replies": [
        {"label": "Привет",
         "text": "Здравствуйте! Сейчас отвечу."},
        {"label": "Спасибо",
         "text": "Спасибо за покупку! Буду рад видеть вас снова."},
        {"label": "Ждите код",
         "text": "Отправьте в чат команду !mailcode — бот вышлет код."},
    ],

    # ─── Перевод ───
    "translate_enabled": False,
    "translate_provider": "libre",   # libre | google | deepl
    # LibreTranslate: https://libretranslate.com/translate (или self-hosted).
    "translate_endpoint": "https://libretranslate.com/translate",
    "translate_api_key": "",
    # Целевой язык для входящих (пусто — не переводить).
    "translate_incoming_to": "ru",
    # Целевой язык для исходящих (пусто — не переводить).
    "translate_outgoing_to": "",
    # Не переводить если язык исходника уже такой.
    "translate_skip_same": True,
    # Сохранять кэш переводов между перезапусками (translate_cache.json).
    "translate_cache_persist": True,
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


# --- Bridge state ----------------------------------------------------------
#
# bridges.json формат:
# {
#   "<playerok_chat_id>": {
#     "topic_id": 123,          # message_thread_id в TG
#     "name": "Topic name",
#     "alias": "MyShop",        # аккаунт Playerok (для multi-account)
#     "buyer_username": "...",
#     "buyer_id": "...",
#     "created_at": 1700000000,
#     "last_relay_ts": 1700000000,
#     "closed": false
#   }
# }

_BRIDGE_LOCK = threading.Lock()


def load_bridges() -> dict[str, dict[str, Any]]:
    return common.load_json(BRIDGE_FILE, {}) or {}


def save_bridges(state: dict[str, dict[str, Any]]) -> None:
    common.save_json(BRIDGE_FILE, state)


def get_bridge_for_chat(playerok_chat_id: str) -> dict[str, Any] | None:
    st = load_bridges()
    return st.get(str(playerok_chat_id))


def set_bridge_for_chat(playerok_chat_id: str, entry: dict[str, Any]) -> None:
    with _BRIDGE_LOCK:
        st = load_bridges()
        st[str(playerok_chat_id)] = entry
        save_bridges(st)


def find_playerok_chat_by_topic(topic_id: int) -> str | None:
    st = load_bridges()
    for pid, e in st.items():
        if int(e.get("topic_id") or 0) == int(topic_id):
            return pid
    return None


# --- Translation -----------------------------------------------------------

def _translate_libre(text: str, target: str, *,
                     endpoint: str, api_key: str = "",
                     timeout: float = 8.0) -> tuple[str | None, str | None]:
    """LibreTranslate. Возвращает (translated, detected_src) или (None, None)."""
    try:
        payload: dict[str, Any] = {
            "q": text,
            "source": "auto",
            "target": target,
            "format": "text",
        }
        if api_key:
            payload["api_key"] = api_key
        resp = requests.post(endpoint, json=payload, timeout=timeout)
    except Exception:
        LOGGER.exception("LibreTranslate request failed")
        return (None, None)
    if resp.status_code != 200:
        LOGGER.warning("LibreTranslate HTTP %s: %s",
                       resp.status_code, resp.text[:200])
        return (None, None)
    try:
        data = resp.json()
    except Exception:
        return (None, None)
    translated = data.get("translatedText")
    detected = (data.get("detectedLanguage") or {}).get("language")
    if not translated:
        return (None, None)
    return (str(translated), str(detected) if detected else None)


def _translate_google(text: str, target: str, *,
                      api_key: str, timeout: float = 8.0
                      ) -> tuple[str | None, str | None]:
    """Google Cloud Translation v2."""
    if not api_key:
        return (None, None)
    try:
        resp = requests.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": api_key},
            data={"q": text, "target": target, "format": "text"},
            timeout=timeout,
        )
    except Exception:
        LOGGER.exception("Google Translate request failed")
        return (None, None)
    if resp.status_code != 200:
        LOGGER.warning("Google Translate HTTP %s: %s",
                       resp.status_code, resp.text[:200])
        return (None, None)
    try:
        items = resp.json()["data"]["translations"]
        first = items[0]
        return (str(first["translatedText"]),
                first.get("detectedSourceLanguage"))
    except Exception:
        return (None, None)


def _translate_deepl(text: str, target: str, *,
                     api_key: str, endpoint: str = "",
                     timeout: float = 8.0
                     ) -> tuple[str | None, str | None]:
    """DeepL. endpoint: api-free.deepl.com или api.deepl.com."""
    if not api_key:
        return (None, None)
    url = endpoint or "https://api-free.deepl.com/v2/translate"
    try:
        resp = requests.post(
            url,
            data={"text": text, "target_lang": target.upper()},
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            timeout=timeout,
        )
    except Exception:
        LOGGER.exception("DeepL request failed")
        return (None, None)
    if resp.status_code != 200:
        LOGGER.warning("DeepL HTTP %s: %s",
                       resp.status_code, resp.text[:200])
        return (None, None)
    try:
        item = resp.json()["translations"][0]
        return (str(item["text"]),
                (item.get("detected_source_language") or "").lower() or None)
    except Exception:
        return (None, None)


def translate_text(text: str, target: str, cfg: dict[str, Any]
                   ) -> tuple[str | None, str | None]:
    """Универсальный перевод. Возвращает (translated, detected_src).

    Уважает кэш + skip_same. None для translated означает «не переводить»
    (либо ошибка, либо тот же язык, либо переводчик выключен).
    """
    if not text or not target:
        return (None, None)
    target = target.lower().strip()
    if not cfg.get("translate_enabled"):
        return (None, None)
    persist = bool(cfg.get("translate_cache_persist", True))
    if persist:
        _load_translate_cache()
    key = (cfg.get("translate_provider", "libre"), target, text)
    with _TRANSLATE_LOCK:
        cached = _TRANSLATE_CACHE.get(key)
    if cached is not None:
        return (cached, None)

    provider = cfg.get("translate_provider", "libre")
    if provider == "libre":
        translated, src = _translate_libre(
            text, target,
            endpoint=cfg.get("translate_endpoint")
            or "https://libretranslate.com/translate",
            api_key=cfg.get("translate_api_key", ""),
        )
    elif provider == "google":
        translated, src = _translate_google(
            text, target, api_key=cfg.get("translate_api_key", ""))
    elif provider == "deepl":
        translated, src = _translate_deepl(
            text, target,
            api_key=cfg.get("translate_api_key", ""),
            endpoint=cfg.get("translate_endpoint", ""))
    else:
        LOGGER.warning("Unknown translate_provider: %s", provider)
        return (None, None)

    if translated is None:
        return (None, src)

    # Skip-same: переводчик вернул тот же текст или язык совпал.
    if cfg.get("translate_skip_same", True):
        if src and src.lower() == target:
            return (None, src)
        if translated.strip() == text.strip():
            return (None, src)

    global _TRANSLATE_DIRTY
    with _TRANSLATE_LOCK:
        if len(_TRANSLATE_CACHE) >= _TRANSLATE_CACHE_MAX:
            # Простой trim: удалить ~10% случайных записей.
            for k in list(_TRANSLATE_CACHE.keys())[: _TRANSLATE_CACHE_MAX // 10]:
                _TRANSLATE_CACHE.pop(k, None)
        _TRANSLATE_CACHE[key] = translated
        _TRANSLATE_DIRTY = True
    if persist:
        _save_translate_cache()
    return (translated, src)


# --- TG-bridge helpers -----------------------------------------------------

def _chat_topic_name(chat: Any, *, prefix: str = "",
                     max_len: int = 50) -> str:
    """Имя топика TG для Playerok-чата."""
    users = getattr(chat, "users", None) or []
    buyer = None
    for u in users:
        # У владельца магазина обычно есть owner=True; берём первого «не нас».
        # Без дополнительного контекста просто берём первого пользователя.
        username = getattr(u, "username", None)
        if username:
            buyer = username
            break
    cid = getattr(chat, "id", "")
    short = (buyer or "")[:32] or f"chat-{str(cid)[:8]}"
    name = f"{prefix}{short}" if prefix else short
    return name[:max_len] or "chat"


def _buyer_username_from_chat(chat: Any) -> str:
    users = getattr(chat, "users", None) or []
    for u in users:
        username = getattr(u, "username", None)
        if username:
            return str(username)
    return ""


def _ensure_topic(bot: Any, cfg: dict[str, Any], playerok_chat: Any,
                  *, alias: str = "") -> dict[str, Any] | None:
    """Возвращает запись bridge для playerok_chat. Создаёт топик, если надо."""
    if not cfg.get("bridge_enabled"):
        return None
    bridge_chat = int(cfg.get("bridge_chat_id") or 0)
    if not bridge_chat:
        return None
    playerok_chat_id = getattr(playerok_chat, "id", None)
    if not playerok_chat_id:
        return None
    existing = get_bridge_for_chat(str(playerok_chat_id))
    if existing and not existing.get("closed"):
        return existing
    # Если топик был закрыт (закончили сделку) — попытаться переоткрыть.
    if existing and existing.get("closed"):
        thread_id = int(existing.get("topic_id") or 0)
        if thread_id:
            try:
                bot.reopen_forum_topic(bridge_chat, thread_id)
                existing["closed"] = False
                existing["reopened_at"] = int(time.time())
                set_bridge_for_chat(str(playerok_chat_id), existing)
                return existing
            except Exception:
                LOGGER.exception(
                    "bridge: reopen_forum_topic failed, will create new")

    prefix = cfg.get("bridge_topic_prefix") or ""
    if prefix and not prefix.endswith(" "):
        prefix = f"{prefix} "
    name = _chat_topic_name(playerok_chat, prefix=prefix)
    try:
        topic = bot.create_forum_topic(bridge_chat, name)
        thread_id = int(getattr(topic, "message_thread_id", 0) or 0)
    except Exception:
        LOGGER.exception("bridge: create_forum_topic failed (chat=%s)",
                         bridge_chat)
        return None
    if not thread_id:
        return None
    entry = {
        "topic_id": thread_id,
        "name": name,
        "alias": alias,
        "buyer_username": _buyer_username_from_chat(playerok_chat),
        "created_at": int(time.time()),
        "last_relay_ts": 0,
        "closed": False,
    }
    set_bridge_for_chat(str(playerok_chat_id), entry)
    # Шапка топика c quick-reply кнопками.
    qr_rows = _quick_reply_keyboard(str(playerok_chat_id), cfg)
    header_kb = tg_types.InlineKeyboardMarkup() if qr_rows else None
    if header_kb is not None:
        for row in qr_rows:
            header_kb.row(*row)
    try:
        bot.send_message(
            bridge_chat,
            _bridge_header_text(playerok_chat_id, entry),
            parse_mode="Markdown",
            reply_markup=header_kb,
            message_thread_id=thread_id,
        )
    except Exception:
        LOGGER.exception("bridge: header post failed")
    return entry


def _bridge_header_text(playerok_chat_id: str, entry: dict[str, Any]) -> str:
    buyer = entry.get("buyer_username") or "?"
    alias = entry.get("alias") or "—"
    return (
        "🌉 *TG-bridge*\n"
        f"Чат Playerok: `{playerok_chat_id}`\n"
        f"Покупатель: `{buyer}`\n"
        f"Аккаунт: `{alias}`\n\n"
        "_Ответы в этом топике уходят покупателю в Playerok._"
    )


def _quick_reply_keyboard(playerok_chat_id: str,
                          cfg: dict[str, Any]) -> list[list[Any]]:
    """Возвращает кнопки quick-replies как список рядов."""
    qrs = cfg.get("quick_replies") or []
    if not qrs:
        return []
    cols = max(1, int(cfg.get("bridge_quick_reply_columns", 2) or 2))
    buttons = []
    for idx, qr in enumerate(qrs):
        label = (qr.get("label") or qr.get("text") or f"#{idx + 1}")[:24]
        buttons.append(tg_types.InlineKeyboardButton(
            label, callback_data=f"cm:qrsend:{playerok_chat_id}:{idx}"))
    rows: list[list[Any]] = []
    for i in range(0, len(buttons), cols):
        rows.append(buttons[i:i + cols])
    return rows


def _format_incoming_for_topic(sender: str, text: str,
                               translated: str | None,
                               detected_src: str | None) -> str:
    """Сообщение от покупателя → Markdown для топика."""
    head = f"👤 *{common.md_escape(sender)}*"
    if detected_src and detected_src != "auto":
        head += f"  _({detected_src})_"
    body = common.md_escape(text or "")
    out = f"{head}\n{body}"
    if translated:
        out += f"\n\n🌍 _{common.md_escape(translated)}_"
    return out


# --- Reverse bridge: TG-topic → Playerok ----------------------------------

def _is_bridge_reply(message: Any, ctx: PluginContext) -> bool:
    """True если message — текст из bridge-топика (не от самого бота)."""
    try:
        cfg = get_config()
        if not cfg.get("bridge_enabled"):
            return False
        bridge_chat = int(cfg.get("bridge_chat_id") or 0)
        if not bridge_chat:
            return False
        if int(getattr(message.chat, "id", 0)) != bridge_chat:
            return False
        thread = getattr(message, "message_thread_id", None)
        if not thread:
            return False
        # Игнорим echo от самого бота.
        from_user = getattr(message, "from_user", None)
        if from_user and getattr(from_user, "is_bot", False):
            return False
        return find_playerok_chat_by_topic(int(thread)) is not None
    except Exception:
        return False


def _handle_bridge_topic_text(bot: Any, ctx: PluginContext,
                              message: Any) -> None:
    """Перенаправляет текст из bridge-топика в соответствующий чат Playerok."""
    cfg = get_config()
    thread = int(getattr(message, "message_thread_id", 0) or 0)
    if not thread:
        return
    playerok_chat_id = find_playerok_chat_by_topic(thread)
    if not playerok_chat_id:
        return
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return
    target_lang = (cfg.get("translate_outgoing_to") or "").strip().lower()
    send_text = text
    if target_lang:
        tr, _ = translate_text(text, target_lang, cfg)
        if tr:
            send_text = tr
    if not ctx.playerok_acc:
        try:
            bot.send_message(message.chat.id,
                             "❌ Playerok-аккаунт не подключён.",
                             message_thread_id=thread)
        except Exception:
            pass
        return
    try:
        ctx.playerok_acc.send_message(playerok_chat_id, send_text)
    except Exception as exc:
        LOGGER.exception("bridge: send_message to playerok failed")
        try:
            bot.send_message(message.chat.id, f"❌ Ошибка: {exc}",
                             message_thread_id=thread)
        except Exception:
            pass


def _download_tg_file(bot: Any, file_id: str, suggested_name: str) -> str | None:
    """Качает файл из TG и кладёт в storage/plugins/chat_manager/tmp."""
    try:
        info = bot.get_file(file_id)
        blob = bot.download_file(info.file_path)
    except Exception:
        LOGGER.exception("bridge: download_file failed")
        return None
    tmp_dir = os.path.join(STORAGE_DIR, "tmp")
    try:
        common.ensure_dir(tmp_dir)
    except Exception:
        LOGGER.exception("bridge: ensure_dir failed")
        return None
    safe_name = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_"
        for ch in (suggested_name or "file")
    ) or "file"
    path = os.path.join(tmp_dir, f"{int(time.time() * 1000)}_{safe_name}")
    try:
        with open(path, "wb") as fp:
            fp.write(blob)
    except Exception:
        LOGGER.exception("bridge: tmp file write failed")
        return None
    return path


def _handle_bridge_topic_photo(bot: Any, ctx: PluginContext,
                               message: Any) -> None:
    """Перенаправляет фото из bridge-топика в Playerok."""
    cfg = get_config()
    thread = int(getattr(message, "message_thread_id", 0) or 0)
    if not thread:
        return
    playerok_chat_id = find_playerok_chat_by_topic(thread)
    if not playerok_chat_id:
        return
    photos = getattr(message, "photo", None) or []
    if not photos:
        return
    file_id = getattr(photos[-1], "file_id", None)
    if not file_id:
        return
    path = _download_tg_file(bot, file_id, "photo.jpg")
    if not path:
        try:
            bot.send_message(message.chat.id, "❌ Не удалось скачать фото.",
                             message_thread_id=thread)
        except Exception:
            pass
        return

    caption = (getattr(message, "caption", None) or "").strip()
    if caption:
        target_lang = (cfg.get("translate_outgoing_to") or "").strip().lower()
        if target_lang:
            tr, _ = translate_text(caption, target_lang, cfg)
            if tr:
                caption = tr
    if not ctx.playerok_acc:
        try:
            bot.send_message(message.chat.id,
                             "❌ Playerok-аккаунт не подключён.",
                             message_thread_id=thread)
        except Exception:
            pass
        return
    try:
        ctx.playerok_acc.send_message(
            playerok_chat_id, caption, photo_file_paths=[path])
    except Exception as exc:
        LOGGER.exception("bridge: send photo to playerok failed")
        try:
            bot.send_message(message.chat.id, f"❌ Ошибка: {exc}",
                             message_thread_id=thread)
        except Exception:
            pass
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _handle_bridge_topic_document(bot: Any, ctx: PluginContext,
                                  message: Any) -> None:
    """Документы из топика: только если это картинка — релеим как фото."""
    doc = getattr(message, "document", None)
    if not doc:
        return
    mime = (getattr(doc, "mime_type", "") or "").lower()
    file_name = getattr(doc, "file_name", "") or "doc"
    if not mime.startswith("image/"):
        thread = int(getattr(message, "message_thread_id", 0) or 0)
        try:
            bot.send_message(
                message.chat.id,
                ("⚠ Документ `" + file_name +
                 "` не поддерживается. Playerok принимает только картинки."),
                parse_mode="Markdown",
                message_thread_id=thread or None,
            )
        except Exception:
            pass
        return

    cfg = get_config()
    thread = int(getattr(message, "message_thread_id", 0) or 0)
    if not thread:
        return
    playerok_chat_id = find_playerok_chat_by_topic(thread)
    if not playerok_chat_id:
        return
    file_id = getattr(doc, "file_id", None)
    if not file_id:
        return
    path = _download_tg_file(bot, file_id, file_name)
    if not path:
        try:
            bot.send_message(message.chat.id,
                             "❌ Не удалось скачать документ.",
                             message_thread_id=thread)
        except Exception:
            pass
        return

    caption = (getattr(message, "caption", None) or "").strip()
    if caption:
        target_lang = (cfg.get("translate_outgoing_to") or "").strip().lower()
        if target_lang:
            tr, _ = translate_text(caption, target_lang, cfg)
            if tr:
                caption = tr
    if not ctx.playerok_acc:
        try:
            bot.send_message(message.chat.id,
                             "❌ Playerok-аккаунт не подключён.",
                             message_thread_id=thread)
        except Exception:
            pass
        return
    try:
        ctx.playerok_acc.send_message(
            playerok_chat_id, caption, photo_file_paths=[path])
    except Exception as exc:
        LOGGER.exception("bridge: send document(image) to playerok failed")
        try:
            bot.send_message(message.chat.id, f"❌ Ошибка: {exc}",
                             message_thread_id=thread)
        except Exception:
            pass
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# --- Plugin metadata -------------------------------------------------------

PLUGIN = Plugin(
    id="chat_manager",
    name="\u0427\u0430\u0442-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440",
    icon="\U0001f4ac",
    description=(
        "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u0447\u0430\u0442\u043e\u0432 "
        "\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439 "
        "\u0447\u0435\u0440\u0435\u0437 Telegram. /chat \u0434\u043b\u044f \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
    ),
    instruction=(
        "*\U0001f4ac \u0427\u0430\u0442-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440*\n\n"
        "*\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d:*\n"
        "- \u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0441\u043f\u0438\u0441\u043e\u043a "
        "\u043d\u0435\u0434\u0430\u0432\u043d\u0438\u0445 \u0447\u0430\u0442\u043e\u0432.\n"
        "- \u041f\u043e\u0437\u0432\u043e\u043b\u044f\u0435\u0442 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0442\u044c "
        "\u0442\u0435\u043a\u0441\u0442\u043e\u0432\u044b\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f "
        "\u0438 \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u044f.\n"
        "- \u0423\u0432\u0435\u0434\u043e\u043c\u043b\u044f\u0435\u0442 \u043e "
        "\u043d\u043e\u0432\u044b\u0445 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f\u0445.\n\n"
        "*\u041a\u0430\u043a \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c:*\n"
        "1. `/chat` - \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a "
        "\u0447\u0430\u0442\u043e\u0432.\n"
        "2. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0447\u0430\u0442 \u0434\u043b\u044f "
        "\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0430.\n"
        "3. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043e\u0442\u0432\u0435\u0442."
    ),
    default_enabled=True,
    keywords=("\u0447\u0430\u0442", "chat", "\u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435"),
)


# --- Handler ---------------------------------------------------------------

class Handler:
    """Handler for the chat_manager plugin."""

    def setup(self, ctx: PluginContext) -> None:
        get_config()

    def register_telegram(self, ctx: PluginContext) -> None:
        bot = ctx.bot
        admin_id = ctx.admin_id
        wait_state: dict[int, dict[str, Any]] = {}

        @bot.message_handler(commands=["chat"])
        def cmd_chat(message):
            if message.from_user.id != admin_id:
                return
            _send_chat_list(bot, ctx, message.chat.id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cm:"))
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

            if action == "list":
                _send_chat_list(bot, ctx, chat_id, msg_id)
            elif action == "view" and len(parts) >= 3:
                playerok_chat_id = parts[2]
                _send_chat_messages(bot, ctx, chat_id, playerok_chat_id, msg_id)
            elif action == "reply" and len(parts) >= 3:
                playerok_chat_id = parts[2]
                wait_state[chat_id] = {"step": "wait_reply", "target_chat": playerok_chat_id}
                bot.send_message(chat_id, "\u270f\ufe0f \u0412\u0432\u0435\u0434\u0438\u0442\u0435 "
                                 "\u0442\u0435\u043a\u0441\u0442 \u043e\u0442\u0432\u0435\u0442\u0430:")
            elif action == "photo" and len(parts) >= 3:
                playerok_chat_id = parts[2]
                wait_state[chat_id] = {"step": "wait_photo", "target_chat": playerok_chat_id}
                bot.send_message(chat_id, "\U0001f4f7 \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 "
                                 "\u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435:")
            elif action == "toggle":
                cfg = get_config()
                cfg["notify_new_messages"] = not cfg.get("notify_new_messages", True)
                save_config(cfg)
                _send_settings_panel(bot, chat_id, msg_id)
            elif action == "settings":
                _send_settings_panel(bot, chat_id, msg_id)
            elif action == "bridge":
                _send_bridge_panel(bot, chat_id, msg_id)
            elif action == "qr":
                _send_qr_panel(bot, chat_id, msg_id)
            elif action == "tr":
                _send_translate_panel(bot, chat_id, msg_id)
            elif action == "br" and len(parts) >= 3:
                sub = parts[2]
                cfg = get_config()
                if sub == "toggle":
                    cfg["bridge_enabled"] = not cfg.get("bridge_enabled", False)
                    save_config(cfg)
                elif sub == "echo":
                    cfg["bridge_echo_own"] = not cfg.get("bridge_echo_own", False)
                    save_config(cfg)
                elif sub == "closefin":
                    cfg["bridge_close_finished"] = not cfg.get(
                        "bridge_close_finished", True)
                    save_config(cfg)
                elif sub == "onlybuyer":
                    cfg["bridge_only_buyer_messages"] = not cfg.get(
                        "bridge_only_buyer_messages", True)
                    save_config(cfg)
                elif sub == "edit" and len(parts) >= 4:
                    field = parts[3]
                    wait_state[chat_id] = {"step": "edit_bridge", "field": field}
                    bot.send_message(
                        chat_id,
                        f"✏️ Введи новое значение для `{field}`. /cancel — отмена.",
                        parse_mode="Markdown",
                    )
                    return
                _send_bridge_panel(bot, chat_id, msg_id)
            elif action == "qrsend" and len(parts) >= 4:
                playerok_chat_id = parts[2]
                try:
                    idx = int(parts[3])
                except ValueError:
                    return
                _quick_reply_send(bot, ctx, chat_id, playerok_chat_id, idx)
            elif action == "qract" and len(parts) >= 3:
                sub = parts[2]
                if sub == "add":
                    wait_state[chat_id] = {"step": "qr_add"}
                    bot.send_message(
                        chat_id,
                        "✏️ Введи шаблон в виде `Метка | Текст ответа`.\n"
                        "Пример: `Привет | Здравствуйте, чем могу помочь?`",
                        parse_mode="Markdown",
                    )
                elif sub == "del" and len(parts) >= 4:
                    try:
                        idx = int(parts[3])
                    except ValueError:
                        return
                    cfg = get_config()
                    qrs = list(cfg.get("quick_replies") or [])
                    if 0 <= idx < len(qrs):
                        qrs.pop(idx)
                        cfg["quick_replies"] = qrs
                        save_config(cfg)
                    _send_qr_panel(bot, chat_id, msg_id)
            elif action == "tract" and len(parts) >= 3:
                sub = parts[2]
                cfg = get_config()
                if sub == "toggle":
                    cfg["translate_enabled"] = not cfg.get(
                        "translate_enabled", False)
                    save_config(cfg)
                elif sub == "skip":
                    cfg["translate_skip_same"] = not cfg.get(
                        "translate_skip_same", True)
                    save_config(cfg)
                elif sub == "prov" and len(parts) >= 4:
                    prov = parts[3]
                    if prov in ("libre", "google", "deepl"):
                        cfg["translate_provider"] = prov
                        save_config(cfg)
                elif sub == "edit" and len(parts) >= 4:
                    field = parts[3]
                    wait_state[chat_id] = {"step": "edit_translate",
                                           "field": field}
                    bot.send_message(
                        chat_id,
                        f"✏️ Введи новое значение для `{field}`. /cancel — отмена.",
                        parse_mode="Markdown",
                    )
                    return
                _send_translate_panel(bot, chat_id, msg_id)

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state
            and wait_state[m.chat.id].get("step") == "wait_reply",
            content_types=["text"])
        def on_reply_text(message):
            state = wait_state.pop(message.chat.id, {})
            target_chat = state.get("target_chat")
            if not target_chat or not ctx.playerok_acc:
                return
            text = (message.text or "").strip()
            if not text:
                return
            try:
                ctx.playerok_acc.send_message(target_chat, text)
                bot.send_message(message.chat.id, "\u2705 \u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 "
                                 "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.")
            except Exception as exc:
                bot.send_message(message.chat.id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

        @bot.message_handler(commands=["cancel"],
                             func=lambda m: m.from_user.id == admin_id
                             and m.chat.id in wait_state)
        def cmd_cancel(message):
            wait_state.pop(message.chat.id, None)
            bot.send_message(message.chat.id,
                             "\u274c \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.")

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state
            and wait_state[m.chat.id].get("step") in (
                "edit_bridge", "edit_translate"),
            content_types=["text"])
        def on_edit_config_value(message):
            state = wait_state.pop(message.chat.id, {})
            field = state.get("field")
            if not field:
                return
            raw = (message.text or "").strip()
            cfg = get_config()
            if field in ("bridge_chat_id", "bridge_quick_reply_columns"):
                try:
                    cfg[field] = int(raw)
                except ValueError:
                    bot.send_message(
                        message.chat.id,
                        "\u274c \u041e\u0436\u0438\u0434\u0430\u044e \u0447\u0438\u0441\u043b\u043e.")
                    return
            else:
                cfg[field] = raw
            save_config(cfg)
            bot.send_message(
                message.chat.id,
                f"\u2705 `{field}` \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e.",
                parse_mode="Markdown")

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state
            and wait_state[m.chat.id].get("step") == "qr_add",
            content_types=["text"])
        def on_qr_add(message):
            wait_state.pop(message.chat.id, None)
            raw = (message.text or "").strip()
            if "|" not in raw:
                bot.send_message(
                    message.chat.id,
                    "\u274c \u0424\u043e\u0440\u043c\u0430\u0442: `\u041c\u0435\u0442\u043a\u0430 | \u0422\u0435\u043a\u0441\u0442`",
                    parse_mode="Markdown")
                return
            label, body = raw.split("|", 1)
            label = label.strip()[:32]
            body = body.strip()
            if not (label and body):
                bot.send_message(message.chat.id, "\u274c \u041f\u0443\u0441\u0442\u044b\u0435 \u043f\u043e\u043b\u044f.")
                return
            cfg = get_config()
            qrs = list(cfg.get("quick_replies") or [])
            qrs.append({"label": label, "text": body})
            cfg["quick_replies"] = qrs
            save_config(cfg)
            bot.send_message(
                message.chat.id,
                f"\u2705 \u0428\u0430\u0431\u043b\u043e\u043d `{common.md_escape(label)}` \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d.",
                parse_mode="Markdown")

        # === Bridge: ответ из TG-топика → Playerok ============================
        @bot.message_handler(
            func=lambda m: _is_bridge_reply(m, ctx),
            content_types=["text"])
        def on_bridge_topic_text(message):
            try:
                _handle_bridge_topic_text(bot, ctx, message)
            except Exception:
                LOGGER.exception("bridge: topic text handler failed")

        @bot.message_handler(
            func=lambda m: _is_bridge_reply(m, ctx),
            content_types=["photo"])
        def on_bridge_topic_photo(message):
            try:
                _handle_bridge_topic_photo(bot, ctx, message)
            except Exception:
                LOGGER.exception("bridge: topic photo handler failed")

        @bot.message_handler(
            func=lambda m: _is_bridge_reply(m, ctx),
            content_types=["document"])
        def on_bridge_topic_document(message):
            try:
                _handle_bridge_topic_document(bot, ctx, message)
            except Exception:
                LOGGER.exception("bridge: topic document handler failed")

        @bot.message_handler(
            func=lambda m: m.from_user.id == admin_id and m.chat.id in wait_state
            and wait_state[m.chat.id].get("step") == "wait_photo",
            content_types=["photo"])
        def on_reply_photo(message):
            state = wait_state.pop(message.chat.id, {})
            target_chat = state.get("target_chat")
            if not target_chat or not ctx.playerok_acc:
                return
            try:
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded = bot.download_file(file_info.file_path)
                tmp_path = os.path.join(STORAGE_DIR, "tmp_photo.jpg")
                common.ensure_dir(STORAGE_DIR)
                with open(tmp_path, "wb") as f:
                    f.write(downloaded)
                ctx.playerok_acc.send_message(target_chat, "", photo_file_paths=[tmp_path])
                bot.send_message(message.chat.id, "\u2705 \u0418\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 "
                                 "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.")
            except Exception as exc:
                bot.send_message(message.chat.id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

        def _send_chat_list(b, context, tg_chat_id, edit_msg_id=None):
            if not context.playerok_acc:
                b.send_message(tg_chat_id, "\u274c \u0410\u043a\u043a\u0430\u0443\u043d\u0442 "
                               "\u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d.")
                return
            try:
                chat_list = context.playerok_acc.get_chats(count=10)
                chats = getattr(chat_list, "chats", []) or []
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")
                return

            if not chats:
                text = "\U0001f4ac \u0427\u0430\u0442\u044b \u043f\u0443\u0441\u0442\u044b."
                kb = None
            else:
                text = "\U0001f4ac *\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0447\u0430\u0442\u044b:*\n"
                kb = tg_types.InlineKeyboardMarkup()
                for c in chats[:10]:
                    cid = getattr(c, "id", "")
                    label = getattr(c, "name", "") or str(cid)[:20]
                    kb.row(tg_types.InlineKeyboardButton(
                        label, callback_data=f"cm:view:{cid}"))
                kb.row(tg_types.InlineKeyboardButton(
                    "\u2699\ufe0f \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438",
                    callback_data="cm:settings"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_chat_messages(b, context, tg_chat_id, playerok_chat_id, edit_msg_id=None):
            text = f"\U0001f4ac \u0427\u0430\u0442 `{playerok_chat_id[:8]}...`\n\n"
            text += "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 " \
                    "\u043a\u043d\u043e\u043f\u043a\u0438 \u043d\u0438\u0436\u0435 \u0434\u043b\u044f " \
                    "\u043e\u0442\u0432\u0435\u0442\u0430."
            cfg = get_config()
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f \u041e\u0442\u0432\u0435\u0442\u0438\u0442\u044c",
                    callback_data=f"cm:reply:{playerok_chat_id}"),
                tg_types.InlineKeyboardButton(
                    "\U0001f4f7 \u0424\u043e\u0442\u043e",
                    callback_data=f"cm:photo:{playerok_chat_id}"),
            )
            qr_rows = _quick_reply_keyboard(playerok_chat_id, cfg)
            for row in qr_rows:
                kb.row(*row)
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="cm:list"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_settings_panel(b, tg_chat_id, edit_msg_id=None):
            cfg = get_config()
            notify = cfg.get("notify_new_messages", True)
            bridge = cfg.get("bridge_enabled", False)
            translate = cfg.get("translate_enabled", False)
            qr_count = len(cfg.get("quick_replies") or [])
            status_notify = "\u2705" if notify else "\u274c"
            status_bridge = "\u2705" if bridge else "\u274c"
            status_tr = "\u2705" if translate else "\u274c"
            text = (
                "\u2699\ufe0f *\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 "
                "\u0447\u0430\u0442-\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440\u0430*\n\n"
                f"\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f: {status_notify}\n"
                f"TG-bridge: {status_bridge}\n"
                f"Quick replies: {qr_count}\n"
                f"\u041f\u0435\u0440\u0435\u0432\u043e\u0434: {status_tr}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            toggle_label = "\u274c \u0412\u044b\u043a\u043b. \u0443\u0432\u0435\u0434\u043e\u043c\u043b." if notify \
                else "\u2705 \u0412\u043a\u043b. \u0443\u0432\u0435\u0434\u043e\u043c\u043b."
            kb.row(tg_types.InlineKeyboardButton(toggle_label, callback_data="cm:toggle"))
            kb.row(tg_types.InlineKeyboardButton(
                "\U0001f309 TG-bridge", callback_data="cm:bridge"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u26a1 Quick replies", callback_data="cm:qr"))
            kb.row(tg_types.InlineKeyboardButton(
                "\U0001f310 \u041f\u0435\u0440\u0435\u0432\u043e\u0434",
                callback_data="cm:tr"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="cm:list"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_bridge_panel(b, tg_chat_id, edit_msg_id=None):
            cfg = get_config()
            enabled = bool(cfg.get("bridge_enabled"))
            echo = bool(cfg.get("bridge_echo_own"))
            closefin = bool(cfg.get("bridge_close_finished", True))
            onlybuyer = bool(cfg.get("bridge_only_buyer_messages", True))
            bridge_chat = cfg.get("bridge_chat_id") or 0
            prefix = cfg.get("bridge_topic_prefix") or ""
            cols = cfg.get("bridge_quick_reply_columns") or 2
            text = (
                "\U0001f309 *TG-bridge*\n\n"
                f"\u0421\u0442\u0430\u0442\u0443\u0441: {'\u2705' if enabled else '\u274c'}\n"
                f"Supergroup ID: `{bridge_chat}`\n"
                f"\u041f\u0440\u0435\u0444\u0438\u043a\u0441 \u0442\u043e\u043f\u0438\u043a\u0430: `{common.md_escape(str(prefix))}`\n"
                f"\u042d\u0445\u043e \u0441\u0432\u043e\u0438\u0445: {'\u2705' if echo else '\u274c'}\n"
                f"\u0417\u0430\u043a\u0440\u044b\u0432\u0430\u0442\u044c finished: {'\u2705' if closefin else '\u274c'}\n"
                f"\u0422\u043e\u043b\u044c\u043a\u043e \u043f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0435\u0439: {'\u2705' if onlybuyer else '\u274c'}\n"
                f"QR \u043a\u043e\u043b\u043e\u043d\u043e\u043a: `{cols}`\n\n"
                "_Supergroup \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0444\u043e\u0440\u0443\u043c\u043e\u043c \u0438 \u0431\u043e\u0442 \u2014 \u0430\u0434\u043c\u0438\u043d \u0432 \u043d\u0451\u043c._"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                ("\u274c \u0412\u044b\u043a\u043b." if enabled else "\u2705 \u0412\u043a\u043b."),
                callback_data="cm:br:toggle"))
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f Group ID", callback_data="cm:br:edit:bridge_chat_id"),
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f \u041f\u0440\u0435\u0444\u0438\u043a\u0441",
                    callback_data="cm:br:edit:bridge_topic_prefix"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    f"\u042d\u0445\u043e: {'on' if echo else 'off'}",
                    callback_data="cm:br:echo"),
                tg_types.InlineKeyboardButton(
                    f"Closefin: {'on' if closefin else 'off'}",
                    callback_data="cm:br:closefin"),
            )
            kb.row(tg_types.InlineKeyboardButton(
                f"\u041f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0438: {'on' if onlybuyer else 'off'}",
                callback_data="cm:br:onlybuyer"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u270f\ufe0f QR \u043a\u043e\u043b\u043e\u043d\u043e\u043a",
                callback_data="cm:br:edit:bridge_quick_reply_columns"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="cm:settings"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_qr_panel(b, tg_chat_id, edit_msg_id=None):
            cfg = get_config()
            qrs = list(cfg.get("quick_replies") or [])
            text = "\u26a1 *Quick replies*\n\n"
            if not qrs:
                text += "_\u041f\u0443\u0441\u0442\u043e._\n"
            else:
                for i, qr in enumerate(qrs):
                    label = qr.get("label", "?")
                    body = qr.get("text", "")
                    text += (f"{i + 1}. *{common.md_escape(label)}* — "
                             f"{common.md_escape(body[:60])}\n")
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                "\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c",
                callback_data="cm:qract:add"))
            for i, qr in enumerate(qrs):
                kb.row(tg_types.InlineKeyboardButton(
                    f"\U0001f5d1 {qr.get('label', '?')[:24]}",
                    callback_data=f"cm:qract:del:{i}"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="cm:settings"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _send_translate_panel(b, tg_chat_id, edit_msg_id=None):
            cfg = get_config()
            enabled = bool(cfg.get("translate_enabled"))
            prov = cfg.get("translate_provider") or "libre"
            ep = cfg.get("translate_endpoint") or ""
            inc = cfg.get("translate_incoming_to") or ""
            outg = cfg.get("translate_outgoing_to") or ""
            skip = bool(cfg.get("translate_skip_same", True))
            text = (
                "\U0001f310 *\u041f\u0435\u0440\u0435\u0432\u043e\u0434*\n\n"
                f"\u0421\u0442\u0430\u0442\u0443\u0441: {'\u2705' if enabled else '\u274c'}\n"
                f"\u041f\u0440\u043e\u0432\u0430\u0439\u0434\u0435\u0440: `{prov}`\n"
                f"Endpoint: `{common.md_escape(ep)}`\n"
                f"API key: {'\u2705 \u0437\u0430\u0434\u0430\u043d' if cfg.get('translate_api_key') else '\u274c \u043d\u0435\u0442'}\n"
                f"Incoming \u2192: `{inc or '\u2014'}`\n"
                f"Outgoing \u2192: `{outg or '\u2014'}`\n"
                f"Skip \u0435\u0441\u043b\u0438 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0435\u0442 \u044f\u0437\u044b\u043a: {'\u2705' if skip else '\u274c'}\n"
            )
            kb = tg_types.InlineKeyboardMarkup()
            kb.row(tg_types.InlineKeyboardButton(
                ("\u274c \u0412\u044b\u043a\u043b." if enabled else "\u2705 \u0412\u043a\u043b."),
                callback_data="cm:tract:toggle"))
            kb.row(
                tg_types.InlineKeyboardButton(
                    f"libre{' \u25cf' if prov == 'libre' else ''}",
                    callback_data="cm:tract:prov:libre"),
                tg_types.InlineKeyboardButton(
                    f"google{' \u25cf' if prov == 'google' else ''}",
                    callback_data="cm:tract:prov:google"),
                tg_types.InlineKeyboardButton(
                    f"deepl{' \u25cf' if prov == 'deepl' else ''}",
                    callback_data="cm:tract:prov:deepl"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f Endpoint",
                    callback_data="cm:tract:edit:translate_endpoint"),
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f API key",
                    callback_data="cm:tract:edit:translate_api_key"),
            )
            kb.row(
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f Incoming \u2192",
                    callback_data="cm:tract:edit:translate_incoming_to"),
                tg_types.InlineKeyboardButton(
                    "\u270f\ufe0f Outgoing \u2192",
                    callback_data="cm:tract:edit:translate_outgoing_to"),
            )
            kb.row(tg_types.InlineKeyboardButton(
                f"Skip same: {'on' if skip else 'off'}",
                callback_data="cm:tract:skip"))
            kb.row(tg_types.InlineKeyboardButton(
                "\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="cm:settings"))
            _send_or_edit(b, tg_chat_id, edit_msg_id, text, kb, parse_mode="Markdown")

        def _quick_reply_send(b, context, tg_chat_id, playerok_chat_id, idx):
            cfg = get_config()
            qrs = list(cfg.get("quick_replies") or [])
            if not (0 <= idx < len(qrs)):
                b.send_message(tg_chat_id, "\u274c QR \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.")
                return
            qr = qrs[idx]
            body = (qr.get("text") or "").strip()
            if not body:
                b.send_message(tg_chat_id, "\u274c \u041f\u0443\u0441\u0442\u043e\u0439 \u0448\u0430\u0431\u043b\u043e\u043d.")
                return
            target_lang = (cfg.get("translate_outgoing_to") or "").strip().lower()
            send_text = body
            if target_lang:
                tr, _ = translate_text(body, target_lang, cfg)
                if tr:
                    send_text = tr
            if not context.playerok_acc:
                return
            try:
                context.playerok_acc.send_message(playerok_chat_id, send_text)
                b.send_message(tg_chat_id, "\u2705 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.")
            except Exception as exc:
                b.send_message(tg_chat_id, f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {exc}")

    def on_event(self, event: Any, ctx: PluginContext) -> bool:
        from playerokapi.enums import EventTypes

        try:
            etype = event.type
        except Exception:
            return False

        # Авто-закрытие топика при завершении сделки.
        finished_events = tuple(
            e for e in (
                getattr(EventTypes, "DEAL_CONFIRMED", None),
                getattr(EventTypes, "DEAL_CONFIRMED_AUTOMATICALLY", None),
                getattr(EventTypes, "DEAL_ROLLED_BACK", None),
            ) if e is not None
        )
        if finished_events and etype in finished_events:
            try:
                self._close_bridge_topic(ctx, event)
            except Exception:
                LOGGER.exception("bridge: close on finished failed")
            return False

        new_message = getattr(EventTypes, "NEW_MESSAGE", None)
        if new_message is None or etype != new_message:
            return False

        cfg = get_config()

        # Кто отправитель, что в тексте, какой Playerok-чат.
        msg = getattr(event, "message", None)
        user = getattr(msg, "user", None) if msg else None
        sender_uid = getattr(user, "id", None) if user else None
        sender_username = (getattr(user, "username", None) if user else None) \
            or getattr(event, "sender_name", "") or "?"
        text_body = (getattr(msg, "text", None) if msg else None) \
            or getattr(event, "text", "") or ""
        chat = getattr(event, "chat", None)
        playerok_chat_id = getattr(chat, "id", None) if chat else None
        own_uid = (getattr(ctx.playerok_acc, "id", None)
                   if ctx.playerok_acc else None)
        is_own = bool(sender_uid and own_uid and sender_uid == own_uid)

        # Старый режим: уведомление админу.
        if cfg.get("notify_new_messages", True) and not is_own:
            notify_text = (
                f"\U0001f4ac *\u041d\u043e\u0432\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435*\n"
                f"\u041e\u0442: {common.md_escape(sender_username)}\n"
                f"\u0422\u0435\u043a\u0441\u0442: {common.md_escape(text_body[:200])}"
            )
            try:
                ctx.bot.send_message(ctx.admin_id, notify_text,
                                     parse_mode="Markdown")
            except Exception:
                try:
                    ctx.bot.send_message(ctx.admin_id, notify_text)
                except Exception:
                    pass

        # TG-bridge: пересылаем в форум-топик.
        if cfg.get("bridge_enabled") and playerok_chat_id and chat:
            if is_own and not cfg.get("bridge_echo_own", False):
                return False
            if cfg.get("bridge_only_buyer_messages", True) and not user:
                return False
            try:
                self._relay_to_topic(
                    ctx, cfg, chat, str(playerok_chat_id),
                    sender_username, text_body, is_own=is_own,
                )
            except Exception:
                LOGGER.exception("bridge relay failed")
        return False

    @staticmethod
    def _relay_to_topic(ctx: PluginContext, cfg: dict[str, Any], chat: Any,
                        playerok_chat_id: str, sender: str, text: str,
                        *, is_own: bool) -> None:
        bridge_chat = int(cfg.get("bridge_chat_id") or 0)
        if not bridge_chat:
            return
        entry = _ensure_topic(ctx.bot, cfg, chat,
                              alias=str(cfg.get("bridge_topic_prefix") or ""))
        if not entry:
            return
        thread_id = int(entry.get("topic_id") or 0)
        if not thread_id:
            return

        translated = None
        detected = None
        target = (cfg.get("translate_incoming_to") or "").strip().lower()
        if not is_own and target and text:
            translated, detected = translate_text(text, target, cfg)

        prefix = "↩️ " if is_own else ""
        body = _format_incoming_for_topic(
            f"{prefix}{sender}", text, translated, detected)
        try:
            ctx.bot.send_message(
                bridge_chat, body, parse_mode="Markdown",
                message_thread_id=thread_id,
            )
        except Exception:
            LOGGER.exception("bridge: post to topic failed")
            return
        entry["last_relay_ts"] = int(time.time())
        set_bridge_for_chat(playerok_chat_id, entry)

    @staticmethod
    def _close_bridge_topic(ctx: PluginContext, event: Any) -> None:
        """Закрывает форум-топик в TG для завершённой сделки."""
        cfg = get_config()
        if not cfg.get("bridge_enabled"):
            return
        if not cfg.get("bridge_close_finished", True):
            return
        bridge_chat = int(cfg.get("bridge_chat_id") or 0)
        if not bridge_chat:
            return
        chat = getattr(event, "chat", None)
        playerok_chat_id = getattr(chat, "id", None) if chat else None
        if not playerok_chat_id:
            return
        entry = get_bridge_for_chat(str(playerok_chat_id))
        if not entry or entry.get("closed"):
            return
        thread_id = int(entry.get("topic_id") or 0)
        if not thread_id:
            return
        # Уведомление в топик.
        try:
            etype = getattr(event, "type", None)
            etype_name = getattr(etype, "name", str(etype))
            ctx.bot.send_message(
                bridge_chat,
                f"🔒 *Сделка завершена* (`{etype_name}`). Топик закрыт.",
                parse_mode="Markdown",
                message_thread_id=thread_id,
            )
        except Exception:
            LOGGER.exception("bridge: notify-close failed")
        # Закрытие топика.
        try:
            ctx.bot.close_forum_topic(bridge_chat, thread_id)
        except Exception:
            LOGGER.exception("bridge: close_forum_topic failed")
        entry["closed"] = True
        entry["closed_at"] = int(time.time())
        set_bridge_for_chat(str(playerok_chat_id), entry)


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
