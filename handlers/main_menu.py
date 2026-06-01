"""Главное меню: /start и обработка кнопок нижней клавиатуры."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import bot, ADMIN_ID, is_admin, main_keyboard


def register(b):
    @b.message_handler(commands=["start", "menu"])
    def cmd_start(message):
        if not is_admin(message.from_user.id):
            b.send_message(message.chat.id, "⛔ Доступ запрещён.")
            return

        text = (
            "🎮 *PlayerokSL — Бот* 🎉\n\n"
            "Добро пожаловать! Выберите раздел:"
        )
        b.send_message(
            message.chat.id,
            text,
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
