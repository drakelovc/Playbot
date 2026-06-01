"""Мои аккаунты: список, добавление, выбор аккаунта."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin, main_keyboard
from core.config import load_config, save_config
import core.playerok_connection as conn


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
        kb = tg_types.InlineKeyboardMarkup()
        kb.row(
            tg_types.InlineKeyboardButton("🔑 Вход через токен", callback_data="login_token"),
            tg_types.InlineKeyboardButton("📧 Вход через почту", callback_data="login_email"),
        )
        kb.add(tg_types.InlineKeyboardButton("🌐 Прокси", callback_data="acc_proxy_setup"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад к аккаунтам", callback_data="back_accounts"))
        b.send_message(
            call.message.chat.id,
            "🎮 *Выберите действие:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    @b.callback_query_handler(func=lambda c: c.data == "login_token")
    def cb_login_token(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            call.message.chat.id,
            "🔑 Чтобы получить токен, введите email от своего аккаунта:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_token_email(b, m))

    @b.callback_query_handler(func=lambda c: c.data == "login_email")
    def cb_login_email(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="back_accounts"))
        msg = b.send_message(
            call.message.chat.id,
            "📧 Введите email вашего аккаунта Playerok:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_login_email(b, m))

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


def _process_token_email(b, message):
    email = message.text.strip() if message.text else ""
    if not email or "@" not in email:
        b.send_message(message.chat.id, "❌ Некорректный email. Попробуйте ещё раз.")
        return
    b.send_message(
        message.chat.id,
        f"📧 Запрос токена отправлен на *{email}*.\n"
        "Введите полученный токен:",
        parse_mode="Markdown",
    )


def _process_login_email(b, message):
    email = message.text.strip() if message.text else ""
    if not email or "@" not in email:
        b.send_message(message.chat.id, "❌ Некорректный email. Попробуйте ещё раз.")
        return
    b.send_message(
        message.chat.id,
        f"📧 Вход через почту: *{email}*\n"
        "Введите пароль:",
        parse_mode="Markdown",
    )
