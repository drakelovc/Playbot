"""Мои товары: активные/завершённые."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
import core.playerok_connection as conn


def register(b):

    @b.message_handler(func=lambda m: m.text == "📦 Мои товары")
    def btn_my_items(message):
        if not is_admin(message.from_user.id):
            return
        _send_items(b, message.chat.id)

    @b.callback_query_handler(func=lambda c: c.data.startswith("items_tab:"))
    def cb_items_tab(call):
        if not is_admin(call.from_user.id):
            return
        tab = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "⏳ Загрузка...")
        _send_items_list(b, call.message.chat.id, tab)


def _send_items(b, chat_id: int):
    text = (
        "📦 *Мои товары*\n\n"
        "Функция позволяет просматривать все свои выставленные товары.\n\n"
        "Выберите категорию:"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.row(
        tg_types.InlineKeyboardButton("🟢 Активные", callback_data="items_tab:active"),
        tg_types.InlineKeyboardButton("🔴 Завершённые", callback_data="items_tab:completed"),
    )
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_items_list(b, chat_id: int, tab: str):
    tab_labels = {"active": "🟢 Активные", "completed": "🔴 Завершённые"}
    title = tab_labels.get(tab, tab)
    items_list = []

    if conn.playerok_acc:
        try:
            result = conn.playerok_acc.get_user_items(count=20)
            if result and hasattr(result, 'items'):
                for item in result.items:
                    name = getattr(item, 'name', '?')
                    price = getattr(item, 'price', 0)
                    price_val = price / 100 if isinstance(price, int) and price > 100 else price
                    items_list.append(f"📦 {name} — {price_val:.2f} ₽")
        except Exception:
            pass

    text = f"{title}\n\n"
    if items_list:
        text += "\n".join(items_list[:20])
    else:
        text += "_(Нет товаров)_"

    kb = tg_types.InlineKeyboardMarkup()
    kb.row(
        tg_types.InlineKeyboardButton("🟢 Активные", callback_data="items_tab:active"),
        tg_types.InlineKeyboardButton("🔴 Завершённые", callback_data="items_tab:completed"),
    )
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
