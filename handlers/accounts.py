"""Мои аккаунты: список, добавление, выбор аккаунта, вход через email."""
from __future__ import annotations

import logging

from telebot import types as tg_types

from core.bot_instance import is_admin, main_keyboard
from core.config import load_config, save_config
from core.helpers import normalize_proxy, check_proxy
import core.playerok_connection as conn
from core.playerok_auth import send_sign_in_code, confirm_sign_in_code

log = logging.getLogger("playerok_bot.accounts")

# Временное хранилище для email-flow (admin_id → email)
_pending_email_login: dict[int, str] = {}


def register(b):

    @b.message_handler(func=lambda m: m.text == "🎮 Мои аккаунты")
    def btn_accounts(message):
        if not is_admin(message.from_user.id):
            return
        _send_accounts_list(b, message.chat.id)

    @b.callback_query_handler(func=lambda c: c.data == "add_account")
    def cb_add_account(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        cfg = load_config()
        proxy = cfg.get("playerok_proxy", "")
        proxy_label = f"🌐 Прокси: ✅ {proxy}" if proxy else "🌐 Прокси: ❌ не установлен"

        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton(proxy_label, callback_data="pre_login_proxy"))
        kb.row(
            tg_types.InlineKeyboardButton("🔑 Вход через токен", callback_data="login_token"),
            tg_types.InlineKeyboardButton("📧 Вход через почту", callback_data="login_email"),
        )
        kb.add(tg_types.InlineKeyboardButton("🍪 Ввести куки вручную", callback_data="login_cookies"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад к аккаунтам", callback_data="back_accounts"))
        b.send_message(
            call.message.chat.id,
            "🎮 *Добавить аккаунт Playerok*\n\n"
            "⚠️ Если Playerok недоступен без прокси — настройте его перед входом.\n\n"
            "Выберите способ входа:",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ── Вход через токен (ручной) ────────────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "login_token")
    def cb_login_token(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            call.message.chat.id,
            "🔑 *Вход через токен*\n\n"
            "Как получить токен:\n"
            "1. Откройте playerok.com в браузере\n"
            "2. Установите расширение EditThisCookie\n"
            "3. Скопируйте значение cookie `token`\n\n"
            "Вставьте токен:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_token_input(b, m))

    # ── Ввести куки вручную ──────────────────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "login_cookies")
    def cb_login_cookies(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            call.message.chat.id,
            "🍪 *Ввести куки вручную*\n\n"
            "Как получить:\n"
            "1. Откройте playerok.com\n"
            "2. F12 → Application → Cookies\n"
            "3. Скопируйте строку:\n"
            "`token=...;__ddg5_=...`\n\n"
            "Вставьте куки:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_cookies_input(b, m))

    # ── Вход через email (автоматический) ────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "login_email")
    def cb_login_email(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            call.message.chat.id,
            "📧 *Вход через почту*\n\n"
            "Бот отправит код подтверждения на вашу почту, "
            "после чего вам нужно будет ввести полученный код.\n\n"
            "Введите email от аккаунта Playerok:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _step_email_entered(b, m))

    # ── Прокси до входа ─────────────────────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "pre_login_proxy")
    def cb_pre_login_proxy(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        cfg = load_config()
        proxy = cfg.get("playerok_proxy", "")
        if proxy:
            text = f"🌐 *Текущий прокси:*\n`{proxy}`\n\nВведите новый или `0` чтобы убрать:"
        else:
            text = "🌐 *Прокси не установлен*\n\nВведите прокси в формате:\n`http://user:pass@ip:port`\n\nИли `0` чтобы пропустить:"
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="add_account"))
        msg = b.send_message(call.message.chat.id, text, parse_mode="Markdown", reply_markup=kb)
        b.register_next_step_handler(msg, lambda m: _process_pre_login_proxy(b, m))

    # ── Переотправка кода ─────────────────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "resend_code")
    def cb_resend_code(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "📧 Повторная отправка...")
        uid = call.from_user.id
        email = _pending_email_login.get(uid)
        if not email:
            b.send_message(call.message.chat.id, "❌ Сессия истекла. Начните заново.")
            return
        cfg = load_config()
        proxy = cfg.get("playerok_proxy", "")
        user_agent = cfg.get("playerok_user_agent", "")
        result = send_sign_in_code(email, proxy=proxy, user_agent=user_agent)
        if result["ok"]:
            b.send_message(call.message.chat.id, f"📧 Код повторно отправлен на *{email}*", parse_mode="Markdown")
        else:
            b.send_message(call.message.chat.id, f"❌ Ошибка: {result['error']}")

    # ── Навигация ────────────────────────────────────────────────────

    @b.callback_query_handler(func=lambda c: c.data == "back_accounts")
    def cb_back_accounts(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        _send_accounts_list(b, call.message.chat.id)

    @b.callback_query_handler(func=lambda c: c.data.startswith("select_acc:"))
    def cb_select_account(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        acc_name = call.data.split(":", 1)[1]
        _send_account_menu(b, call.message.chat.id, acc_name)


# ── Список аккаунтов ────────────────────────────────────────────────

def _send_accounts_list(b, chat_id: int):
    status = conn.playerok_status
    username = status.get("username")

    kb = tg_types.InlineKeyboardMarkup()
    text = "🎮 Выберите аккаунт для работы:\n"

    if username:
        kb.add(tg_types.InlineKeyboardButton(
            f"👤 {username}",
            callback_data=f"select_acc:{username}",
        ))
    else:
        text += "\n_(Нет подключённых аккаунтов)_"

    kb.add(tg_types.InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_account_menu(b, chat_id: int, acc_name: str):
    status = conn.playerok_status
    reviews = status.get("reviews_count", 0)
    rating = status.get("rating")
    rating_str = f"{rating}" if rating else "—"

    text = (
        f"👤 *{acc_name}*\n\n"
        f"⭐ Отзывы: {reviews}\n"
        f"📊 Рейтинг: {rating_str}\n"
        f"🌐 Прокси: {'✅' if status.get('connected') else '❌'}\n"
    )

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🔄 Обновить данные", callback_data=f"acc_refresh:{acc_name}"))
    kb.row(
        tg_types.InlineKeyboardButton("⚙️ Автопроцессы", callback_data=f"acc_auto:{acc_name}"),
        tg_types.InlineKeyboardButton("✋ Ручные операции", callback_data=f"acc_manual:{acc_name}"),
    )
    kb.add(tg_types.InlineKeyboardButton("👤 Профиль", callback_data=f"acc_profile:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("📊 Статистика", callback_data=f"acc_stats:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("⚙️ Настройки", callback_data=f"acc_settings:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🧩 Модули", callback_data=f"acc_modules:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад к аккаунтам", callback_data="back_accounts"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


# ── Обработка: прокси до входа ───────────────────────────────────────

def _process_pre_login_proxy(b, message):
    text = (message.text or "").strip()
    cfg = load_config()
    if text == "0":
        cfg["playerok_proxy"] = ""
        save_config(cfg)
        b.send_message(message.chat.id, "✅ Прокси убран.")
        _send_add_account(b, message.chat.id)
        return

    if not text:
        _send_add_account(b, message.chat.id)
        return

    normalized = normalize_proxy(text)
    cfg["playerok_proxy"] = normalized
    save_config(cfg)

    if normalized != text:
        b.send_message(message.chat.id, f"🔄 Формат автоисправлен:\n`{normalized}`", parse_mode="Markdown")

    waiting = b.send_message(message.chat.id, "🔍 Проверяю прокси...")
    result = check_proxy(normalized)

    if result["ok"]:
        ip = result.get("ip", "?")
        ms = result.get("ms", "?")
        b.send_message(
            message.chat.id,
            f"✅ *Прокси работает!*\n\n"
            f"🌐 IP: `{ip}`\n"
            f"⚡ Скорость: {ms} мс",
            parse_mode="Markdown",
        )
    else:
        error = result.get("error", "Неизвестная ошибка")
        b.send_message(
            message.chat.id,
            f"❌ *Прокси не работает!*\n\n"
            f"Ошибка: {error}\n\n"
            "Прокси сохранён, но рекомендуется заменить.",
            parse_mode="Markdown",
        )

    _send_add_account(b, message.chat.id)


def _send_add_account(b, chat_id: int):
    cfg = load_config()
    proxy = cfg.get("playerok_proxy", "")
    proxy_label = f"🌐 Прокси: ✅ {proxy}" if proxy else "🌐 Прокси: ❌ не установлен"

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton(proxy_label, callback_data="pre_login_proxy"))
    kb.row(
        tg_types.InlineKeyboardButton("🔑 Вход через токен", callback_data="login_token"),
        tg_types.InlineKeyboardButton("📧 Вход через почту", callback_data="login_email"),
    )
    kb.add(tg_types.InlineKeyboardButton("🍪 Ввести куки вручную", callback_data="login_cookies"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад к аккаунтам", callback_data="back_accounts"))
    b.send_message(
        chat_id,
        "🎮 *Добавить аккаунт Playerok*\n\n"
        "⚠️ Если Playerok недоступен без прокси — настройте его перед входом.\n\n"
        "Выберите способ входа:",
        parse_mode="Markdown",
        reply_markup=kb,
    )


# ── Обработка: ввод токена ───────────────────────────────────────────

def _process_token_input(b, message):
    token = (message.text or "").strip()
    if not token or len(token) < 10:
        b.send_message(message.chat.id, "❌ Некорректный токен. Попробуйте ещё раз.")
        return

    waiting = b.send_message(message.chat.id, "⏳ Подключаюсь с токеном...")

    cfg = load_config()
    cfg["playerok_cookies"] = f"token={token}"
    save_config(cfg)

    _try_connect(b, message.chat.id, cfg)


# ── Обработка: ввод куки ────────────────────────────────────────────

def _process_cookies_input(b, message):
    cookies = (message.text or "").strip()
    if not cookies or "=" not in cookies:
        b.send_message(message.chat.id, "❌ Некорректные куки. Формат: `token=...;__ddg5_=...`", parse_mode="Markdown")
        return

    waiting = b.send_message(message.chat.id, "⏳ Подключаюсь с куками...")

    cfg = load_config()
    cfg["playerok_cookies"] = cookies
    save_config(cfg)

    _try_connect(b, message.chat.id, cfg)


# ── Обработка: email flow ───────────────────────────────────────────

def _step_email_entered(b, message):
    """Шаг 1: пользователь ввёл email → отправляем код на почту."""
    email = (message.text or "").strip().lower()
    if not email or "@" not in email:
        b.send_message(message.chat.id, "❌ Некорректный email. Попробуйте ещё раз.")
        return

    uid = message.from_user.id
    cfg = load_config()
    proxy = cfg.get("playerok_proxy", "")
    user_agent = cfg.get("playerok_user_agent", "")

    waiting = b.send_message(message.chat.id, f"📧 Отправляю код на *{email}*...", parse_mode="Markdown")

    result = send_sign_in_code(email, proxy=proxy, user_agent=user_agent)

    if not result["ok"]:
        b.send_message(
            message.chat.id,
            f"❌ Не удалось отправить код: {result['error']}\n\n"
            "Возможные причины:\n"
            "• Неверный email\n"
            "• Нужен прокси (настройте в ⚙️ Настройки → Прокси)\n"
            "• DDoS-Guard блокирует запрос",
        )
        return

    _pending_email_login[uid] = email

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🔄 Отправить код повторно", callback_data="resend_code"))
    kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))

    msg = b.send_message(
        message.chat.id,
        f"✅ Код отправлен на *{email}*!\n\n"
        "📬 Проверьте почту и введите полученный код:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    b.register_next_step_handler(msg, lambda m: _step_code_entered(b, m))


def _step_code_entered(b, message):
    """Шаг 2: пользователь ввёл код → подтверждаем и получаем cookies."""
    code = (message.text or "").strip()
    uid = message.from_user.id
    email = _pending_email_login.get(uid)

    if not email:
        b.send_message(message.chat.id, "❌ Сессия истекла. Нажмите «📧 Вход через почту» заново.")
        return

    if not code or len(code) < 4:
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("🔄 Отправить код повторно", callback_data="resend_code"))
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            message.chat.id,
            "❌ Некорректный код. Введите код из письма:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _step_code_entered(b, m))
        return

    cfg = load_config()
    proxy = cfg.get("playerok_proxy", "")
    user_agent = cfg.get("playerok_user_agent", "")

    waiting = b.send_message(message.chat.id, "⏳ Проверяю код...")

    result = confirm_sign_in_code(email, code, proxy=proxy, user_agent=user_agent)

    if not result["ok"]:
        attempts = result.get("attempts_left")
        extra = f"\nОсталось попыток: {attempts}" if attempts is not None else ""

        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("🔄 Отправить код повторно", callback_data="resend_code"))
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))

        msg = b.send_message(
            message.chat.id,
            f"❌ Ошибка: {result['error']}{extra}\n\nВведите код ещё раз:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _step_code_entered(b, m))
        return

    # Успешная авторизация
    _pending_email_login.pop(uid, None)

    cookies_str = result.get("cookies", "")
    token = result.get("token", "")

    if not cookies_str and token:
        cookies_str = f"token={token}"

    cfg["playerok_cookies"] = cookies_str
    if user_agent:
        cfg["playerok_user_agent"] = user_agent
    save_config(cfg)

    b.send_message(
        message.chat.id,
        f"✅ Авторизация успешна!\n"
        f"📧 Email: *{email}*\n\n"
        "⏳ Подключаюсь к Playerok...",
        parse_mode="Markdown",
    )

    _try_connect(b, message.chat.id, cfg)


# ── Подключение к Playerok ──────────────────────────────────────────

def _try_connect(b, chat_id: int, cfg: dict):
    """Подключиться к Playerok и запустить listener."""
    from core.event_loop import start_playerok_thread

    if conn.init_playerok(cfg):
        start_playerok_thread()
        username = conn.playerok_status.get("username", "?")
        balance = conn.playerok_status.get("balance")
        bal_str = f"{balance:.2f} ₽" if balance is not None else "—"

        b.send_message(
            chat_id,
            f"✅ *Подключено!*\n\n"
            f"👤 Аккаунт: *{username}*\n"
            f"💰 Баланс: {bal_str}\n\n"
            "Используйте меню для управления.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    else:
        error = conn.playerok_status.get("error", "Неизвестная ошибка")
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("🔄 Попробовать ещё раз", callback_data="add_account"))
        b.send_message(
            chat_id,
            f"❌ Не удалось подключиться:\n`{error}`\n\n"
            "Проверьте куки/прокси и попробуйте снова.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
