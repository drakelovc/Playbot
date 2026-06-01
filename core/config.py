"""Конфигурация бота: загрузка, сохранение, дефолты."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime

CONFIG_FILE = "playerok_config.json"
ACCOUNTS_FOLDER = "accounts"
LOG_FILE = "playerok_bot.log"
CODE_PERIOD = 30

DEFAULT_CONFIG: dict = {
    "playerok_cookies": "",
    "playerok_user_agent": "",
    "playerok_proxy": "",
    # --- Автонастройки ---
    "auto_code": True,
    "auto_confirm": True,
    "auto_restore": False,
    "auto_restore_interval": 30,
    "auto_restore_expired": True,
    "auto_bump": False,
    "auto_bump_interval": 30,
    "smart_bump_enabled": False,
    "smart_bump_ranges": [
        {"days": [1, 2, 3, 4, 5, 6, 7], "from": 19, "to": 2},
    ],
    "bump_tz_offset": 3,
    "daily_summary_enabled": True,
    "daily_summary_hour": 0,
    "metrics_enabled": False,
    "metrics_port": 9101,
    "metrics_bind": "0.0.0.0",
    "quick_replies": [
        "Здравствуйте! Спасибо за заказ.",
        "Сейчас отвечу — одну минуту.",
        "Технические работы, скоро вернусь.",
    ],
    "auto_greeting": True,
    "greeting_text": (
        "👋 Привет! Я автоматический бот.\n"
        "Для получения Steam Guard кода напиши: !code <ник_аккаунта>"
    ),
    "ignore_reminder": True,
    "ignore_reminder_text": "⏰ Вы не ответили. Если вам нужен Steam Guard код, напишите: !code <ник_аккаунта>",
    "ignore_reminder_minutes": 10,
    "confirm_reminder": True,
    "confirm_reminder_text": "📦 Пожалуйста, подтвердите получение товара!",
    "confirm_reminder_minutes": 30,
    "after_confirm_text": "✅ Спасибо за покупку! Буду рад оставленному отзыву 🙏",
    "auto_responder": True,
    "auto_responses": {
        "привет": "👋 Привет! Чем могу помочь?",
        "здравствуйте": "👋 Здравствуйте! Чем могу помочь?",
        "hello": "👋 Hello! How can I help?",
        "помощь": "📖 Доступные команды:\n!code <ник> — получить Steam Guard код",
        "help": "📖 Commands:\n!code <nick> — get Steam Guard code",
    },
    # --- Уведомления ---
    "notify_new_message": True,
    "notify_new_deal": True,
    "notify_deal_confirmed": True,
    "notify_deal_problem": True,
    "notify_code_sent": True,
    "permanent_online": True,
    # --- Автовывод ---
    "auto_withdraw_enabled": False,
    "auto_withdraw_notify": True,
    "auto_withdraw_threshold": 1000,
    "auto_withdraw_requisites": "",
    "auto_withdraw_method": "",
    # --- Чаты ---
    "chat_new_messages": True,
    "chat_notify_system": True,
    "chat_notify_support": True,
    "chat_notify_playerok": True,
    "chat_auto_read": False,
    "chat_ignore_messages": False,
    "chat_commands_enabled": True,
    # --- Черный список ---
    "blacklist": [],
    # --- Привязки ---
    "account_map": {},
    "default_account": "",
    # --- Статистика ---
    "stats": {
        "codes_sent": 0,
        "deals_confirmed": 0,
        "messages_received": 0,
        "deals_total": 0,
        "revenue": 0.0,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    },
    "history": [],
    "greeted_chats": [],
    "reminded_chats": {},
    "enabled_plugins": [
        "autosteamoffline", "autosteamrental", "autowithdraw",
        "chat_manager", "reviews", "deals", "autoconfirm",
        "items", "custom_commands", "proxy_manager",
    ],
}

_db = None

def set_db(db_module):
    global _db
    _db = db_module


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = deepcopy(v)
        if "stats" in data:
            for sk, sv in DEFAULT_CONFIG["stats"].items():
                if sk not in data["stats"]:
                    data["stats"][sk] = sv
        plugins_list = data.get("enabled_plugins")
        if isinstance(plugins_list, list):
            if "steam_guard" in plugins_list:
                plugins_list = [p for p in plugins_list if p != "steam_guard"]
                if "autosteamoffline" not in plugins_list:
                    plugins_list.append("autosteamoffline")
                if "autosteamrental" not in plugins_list:
                    plugins_list.append("autosteamrental")
                data["enabled_plugins"] = plugins_list
        return data
    except Exception:
        return deepcopy(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    if _db is not None:
        try:
            _db.save_file(CONFIG_FILE)
        except Exception:
            pass
