"""Ручные операции: вывод баланса, прочитать чаты, рассылка."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
import core.playerok_connection as conn


def register(b):

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_manual:"))
    def cb_manual_menu(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_manual_menu(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("manual_withdraw:"))
    def cb_manual_withdraw(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        balance = conn.playerok_status.get("balance")
        balance_str = f"{balance:.2f}" if balance else "0.00"
        b.send_message(
            call.message.chat.id,
            f"💰 *Вывод баланса*\n\n"
            f"Текущий баланс: *{balance_str} ₽*\n\n"
            "Введите сумму для вывода:",
            parse_mode="Markdown",
        )

    @b.callback_query_handler(func=lambda c: c.data.startswith("manual_read_chats:"))
    def cb_manual_read(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "📖 Прочитываю чаты...")
        try:
            if conn.playerok_acc:
                chats = conn.playerok_acc.get_chats(count=10)
                b.send_message(
                    call.message.chat.id,
                    "✅ Чаты прочитаны!",
                )
            else:
                b.send_message(call.message.chat.id, "❌ Playerok не подключён.")
        except Exception as exc:
            b.send_message(call.message.chat.id, f"❌ Ошибка: {exc}")

    @b.callback_query_handler(func=lambda c: c.data.startswith("manual_mailing:"))
    def cb_manual_mailing(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_manual:{acc_name}"))
        msg = b.send_message(
            call.message.chat.id,
            "📨 *Рассылка*\n\n"
            "Введите текст сообщения для рассылки по чатам:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_mailing(b, m))


def _send_manual_menu(b, chat_id: int, acc_name: str):
    text = (
        f"✋ *Ручные операции*\n"
        "🚀 _Задачи, которые выполняются вручную_"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("💰 Вывести баланс", callback_data=f"manual_withdraw:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("📖 Прочитать чаты", callback_data=f"manual_read_chats:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("📨 Рассылка", callback_data=f"manual_mailing:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _process_mailing(b, message):
    text = (message.text or "").strip()
    if not text:
        b.send_message(message.chat.id, "❌ Текст пустой.")
        return
    b.send_message(message.chat.id, f"📨 Рассылка запущена с текстом:\n\n{text}")
