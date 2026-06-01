"""Профиль аккаунта: открыть контент, покупки, товары, продажи, отзывы, чаты."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
import core.playerok_connection as conn


def register(b):

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_profile:"))
    def cb_acc_profile(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_acc_profile(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_open_content:"))
    def cb_open_content(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_profile:{acc_name}"))
        msg = b.send_message(
            call.message.chat.id,
            "🔗 Вы можете быстро открыть чат, сделку или товар прямо в боте, "
            "просто вставив ссылку!\n\n"
            "📎 Доступные типы ссылок:\n"
            "🛒 Товар: https://playerok.com/products/---\n"
            "💬 Чат: https://playerok.com/chats/---\n"
            "🤝 Сделка: https://playerok.com/deal/---\n\n"
            "👉 Введите ссылку:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_content_url(b, m))

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_purchases:"))
    def cb_purchases(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_list_view(b, call.message.chat.id, acc_name, "purchases", "🛍 Мои покупки",
                        "Функция позволяет просматривать все свои покупки")

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_my_items:"))
    def cb_my_items(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_list_view(b, call.message.chat.id, acc_name, "items", "📦 Мои товары",
                        "Функция позволяет просматривать все свои выставленные товары")

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_my_sales:"))
    def cb_my_sales(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_sales_view(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_my_reviews:"))
    def cb_my_reviews(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_reviews_view(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_my_chats:"))
    def cb_my_chats(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "⏳ Подождите, идёт обработка...")
        _send_chats_view(b, call.message.chat.id, acc_name)


def _send_acc_profile(b, chat_id: int, acc_name: str):
    text = (
        f"👤 *Профиль*\n"
        f"🚀 _Здесь вы можете видеть и управлять контентом из вашего профиля_"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🔗 Открыть контент", callback_data=f"acc_open_content:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🛍 Мои покупки", callback_data=f"acc_purchases:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("📦 Мои товары", callback_data=f"acc_my_items:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🛒 Мои продажи", callback_data=f"acc_my_sales:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("⭐ Мои отзывы", callback_data=f"acc_my_reviews:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("💬 Мои чаты", callback_data=f"acc_my_chats:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_list_view(b, chat_id: int, acc_name: str, view_type: str, title: str, description: str):
    text = f"{title}\n\n{description}"
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🟢 Активные", callback_data=f"list_active:{view_type}:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🔴 Завершённые", callback_data=f"list_completed:{view_type}:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_profile:{acc_name}"))
    b.send_message(chat_id, text, reply_markup=kb)


def _send_sales_view(b, chat_id: int, acc_name: str):
    text = "🛒 *Мои продажи*\n\nФункция позволяет просматривать все свои продажи"
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("⏳ Выполнение", callback_data=f"sales_status:execution:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("✅ Подтверждение", callback_data=f"sales_status:confirmation:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🎉 Завершено", callback_data=f"sales_status:completed:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("💸 Возврат средств", callback_data=f"sales_status:refund:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_profile:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_reviews_view(b, chat_id: int, acc_name: str):
    text = "⭐ *Мои отзывы*\n\nФункция позволяет просматривать все свои отзывы"
    kb = tg_types.InlineKeyboardMarkup()
    for stars in range(1, 6):
        star_word = {1: "Однозвёздочные", 2: "Двухзвёздочные", 3: "Трёхзвёздочные",
                     4: "Четырёхзвёздочные", 5: "Пятизвёздочные"}
        kb.add(tg_types.InlineKeyboardButton(
            f"{'⭐' * stars} {star_word[stars]}",
            callback_data=f"reviews_stars:{stars}:{acc_name}",
        ))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_profile:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_chats_view(b, chat_id: int, acc_name: str):
    text = "💬 *Мои чаты*\n\n"
    chats_list = []
    if conn.playerok_acc:
        try:
            result = conn.playerok_acc.get_chats(count=10)
            if result and hasattr(result, 'items'):
                for chat in result.items:
                    name = getattr(chat, 'id', '?')
                    chats_list.append(f"💬 `{name}`")
        except Exception:
            pass

    if chats_list:
        text += "\n".join(chats_list)
    else:
        text += "_(Нет активных чатов)_"

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_profile:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _process_content_url(b, message):
    url = (message.text or "").strip()
    if "playerok.com" not in url:
        b.send_message(message.chat.id, "❌ Ссылка должна быть с playerok.com")
        return
    b.send_message(message.chat.id, f"🔗 Открываю: {url}\n\n⏳ Подождите, идёт обработка...")
