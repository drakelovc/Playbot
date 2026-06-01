"""
Playerok Steam Guard Bot — модульная версия.
Бот для автоматизации продаж на Playerok с интеграцией Steam Guard.

Запуск:
    python main.py

Переменные окружения:
    BOT_TOKEN     — токен Telegram-бота
    ADMIN_ID      — Telegram user ID администратора
    DATABASE_URL  — (опционально) URL PostgreSQL для бэкапа на Railway
"""
from __future__ import annotations

import os
import sys
import traceback

# --- Загрузка .env (если есть) ---
ENV_FILE = ".env"
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from core.logging_setup import setup_logging
from core.config import load_config, save_config, set_db, CONFIG_FILE, ACCOUNTS_FOLDER
from core.bot_instance import init_bot, BOT_TOKEN, ADMIN_ID
import core.playerok_connection as conn
from core.event_loop import set_context, start_playerok_thread
from core.background import start_background_threads

import plugins as _plugins

# --- Логирование ---
log = setup_logging()

# --- Postgres-бэкап (Railway) ---
try:
    import db as _db
    set_db(_db)
except ImportError:
    _db = None


def main():
    token = os.getenv("BOT_TOKEN", "")
    admin_id = int(os.getenv("ADMIN_ID", "0"))

    if not token:
        log.error("BOT_TOKEN не задан! Установите переменную окружения.")
        sys.exit(1)
    if not admin_id:
        log.error("ADMIN_ID не задан! Установите переменную окружения.")
        sys.exit(1)

    log.info("=" * 50)
    log.info("Playerok Steam Guard Bot запускается")
    log.info("Python: %s", sys.version)
    log.info("Admin ID: %s", admin_id)

    # --- Postgres ---
    if _db is not None:
        try:
            if _db.init():
                _db.hydrate([CONFIG_FILE, ACCOUNTS_FOLDER])
                _db.start_sync_loop([CONFIG_FILE, ACCOUNTS_FOLDER])
        except Exception:
            log.exception("DB init failed — продолжаю без Postgres-бэкапа")

    # --- Конфиг ---
    cfg = load_config()
    save_config(cfg)

    # --- Telegram Bot ---
    bot = init_bot(token, admin_id)

    # --- Регистрация хендлеров ---
    from handlers import main_menu, profile, accounts, auto_processes
    from handlers import manual_ops, account_profile, statistics
    from handlers import settings, modules, sales, items, chats, actions

    main_menu.register(bot)
    profile.register(bot)
    accounts.register(bot)
    auto_processes.register(bot)
    manual_ops.register(bot)
    account_profile.register(bot)
    statistics.register(bot)
    settings.register(bot)
    modules.register(bot)
    sales.register(bot)
    items.register(bot)
    chats.register(bot)
    actions.register(bot)

    # --- Контекст плагинов ---
    plugin_ctx = _plugins.PluginContext(
        playerok_acc=None,
        bot=bot,
        admin_id=admin_id,
        get_config=load_config,
        save_config=save_config,
        log=log,
    )
    _plugins.register_telegram_all(plugin_ctx)
    _plugins.setup_all(plugin_ctx)
    _plugins.start_background_all(plugin_ctx)

    # --- Event loop ---
    set_context(bot, admin_id, plugin_ctx, _plugins)

    # --- Playerok ---
    if cfg.get("playerok_cookies"):
        log.info("Куки найдены, подключаюсь к Playerok...")
        if conn.init_playerok(cfg):
            start_playerok_thread()
        else:
            log.warning("Не удалось подключиться: %s", conn.playerok_status.get("error"))
    else:
        log.info("Куки не заданы. Отправь /cookies в Telegram.")

    # --- Фоновые задачи ---
    start_background_threads()

    # --- Polling ---
    log.info("Telegram бот запущен. Ожидаю сообщений...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        error_msg = traceback.format_exc()
        log.error("БОТ УПАЛ!\n%s", error_msg)
        raise
