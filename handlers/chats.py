"""Мои чаты: просмотр и управление чатами Playerok."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
import core.playerok_connection as conn


def register(b):

    @b.message_handler(func=lambda m: m.text == "💬 Мои чаты")
    def btn_my_chats(message):
        if not is_admin(message.from_user.id):
            return
        _send_chats(b, message.chat.id)

    @b.callback_query_handler(func=lambda c: c.data.startswith("open_chat:"))
    def cb_open_chat(call):
        if not is_admin(call.from_user.id):
            return
        chat_id_str = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "⏳ Загрузка чата...")
        _send_chat_detail(b, call.message.chat.id, chat_id_str)


def _send_chats(b, chat_id: int):
    text = "💬 *Мои чаты*\n\n"
    chats_list = []

    if conn.playerok_acc:
        try:
            result = conn.playerok_acc.get_chats(count=15)
            if result and hasattr(result, 'items'):
                for chat in result.items:
                    cid = getattr(chat, 'id', '?')
                    chats_list.append((str(cid), str(cid)))
        except Exception:
            pass

    kb = tg_types.InlineKeyboardMarkup()
    if chats_list:
        text += f"Найдено чатов: {len(chats_list)}\n"
        for cid, label in chats_list[:15]:
            kb.add(tg_types.InlineKeyboardButton(f"💬 Чат {label}", callback_data=f"open_chat:{cid}"))
    else:
        text += "_(Нет активных чатов)_"

    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_chat_detail(b, chat_id: int, playerok_chat_id: str):
    text = f"💬 *Чат:* `{playerok_chat_id}`\n\n"

    messages_list = []
    if conn.playerok_acc:
        try:
            msgs = conn.playerok_acc.get_chat_messages(playerok_chat_id, count=5)
            if msgs and hasattr(msgs, 'items'):
                for msg in msgs.items:
                    user = getattr(msg, 'user', None)
                    username = getattr(user, 'username', '?') if user else '?'
                    msg_text = getattr(msg, 'text', '') or ''
                    messages_list.append(f"*{username}:* {msg_text}")
        except Exception:
            pass

    if messages_list:
        text += "\n".join(messages_list[-5:])
    else:
        text += "_(Нет сообщений)_"

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("💬 Ответить", callback_data=f"reply:{playerok_chat_id}"))
    kb.add(tg_types.InlineKeyboardButton("🔑 Отправить код", callback_data=f"pickcode:{playerok_chat_id}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ К списку чатов", callback_data="back_chats_list"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
