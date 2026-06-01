"""Единственный экземпляр Telegram бота и admin_id."""
from __future__ import annotations

import os

import telebot
from telebot import types as tg_types

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot: telebot.TeleBot | None = None


def init_bot(token: str | None = None, admin_id: int | None = None):
    global bot, BOT_TOKEN, ADMIN_ID
    if token:
        BOT_TOKEN = token
    if admin_id:
        ADMIN_ID = admin_id
    bot = telebot.TeleBot(BOT_TOKEN)
    return bot


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def main_keyboard() -> tg_types.ReplyKeyboardMarkup:
    """Persistent нижняя клавиатура в стиле PlayerokSL."""
    kb = tg_types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        tg_types.KeyboardButton("👤 Профиль"),
        tg_types.KeyboardButton("🎮 Мои аккаунты"),
    )
    kb.row(
        tg_types.KeyboardButton("👑 Подписка"),
        tg_types.KeyboardButton("📊 Реферальная система"),
    )
    kb.row(
        tg_types.KeyboardButton("🛒 Мои продажи"),
        tg_types.KeyboardButton("💬 Мои чаты"),
        tg_types.KeyboardButton("📦 Мои товары"),
    )
    return kb
