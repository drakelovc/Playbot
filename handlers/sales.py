"""Мои продажи: кнопка из нижнего persistent-меню."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
import core.playerok_connection as conn


def register(b):

    @b.message_handler(func=lambda m: m.text == "🛒 Мои продажи")
    def btn_my_sales(message):
        if not is_admin(message.from_user.id):
            return
        _send_sales(b, message.chat.id)

    @b.callback_query_handler(func=lambda c: c.data.startswith("sales_status:"))
    def cb_sales_status(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":")
        status = parts[1]
        acc_name = parts[2] if len(parts) > 2 else ""
        b.answer_callback_query(call.id, "⏳ Загрузка...")
        _send_sales_by_status(b, call.message.chat.id, status, acc_name)

    @b.callback_query_handler(func=lambda c: c.data == "back_sales_menu")
    def cb_back_sales(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        _send_sales(b, call.message.chat.id)


def _send_sales(b, chat_id: int):
    text = (
        "🛒 *Мои продажи*\n\n"
        "Функция позволяет просматривать все свои продажи.\n\n"
        "Выберите статус:"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("⏳ Выполнение", callback_data="sales_status:execution:"))
    kb.add(tg_types.InlineKeyboardButton("✅ Подтверждение", callback_data="sales_status:confirmation:"))
    kb.add(tg_types.InlineKeyboardButton("🎉 Завершено", callback_data="sales_status:completed:"))
    kb.add(tg_types.InlineKeyboardButton("💸 Возврат средств", callback_data="sales_status:refund:"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_sales_by_status(b, chat_id: int, status: str, acc_name: str):
    status_map = {
        "execution": "⏳ Выполнение",
        "confirmation": "✅ Подтверждение",
        "completed": "🎉 Завершено",
        "refund": "💸 Возврат средств",
    }
    title = status_map.get(status, status)
    text = f"{title}\n\n_(Нет данных)_"
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data="back_sales_menu"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
