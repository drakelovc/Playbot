"""Профиль: личный кабинет, финансы, подписка, реферальная система."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin, main_keyboard, ADMIN_ID
from core.config import load_config
import core.playerok_connection as conn


def register(b):

    @b.message_handler(func=lambda m: m.text == "👤 Профиль")
    def btn_profile(message):
        if not is_admin(message.from_user.id):
            return
        _send_profile(b, message.chat.id)

    @b.message_handler(func=lambda m: m.text == "👑 Подписка")
    def btn_subscription(message):
        if not is_admin(message.from_user.id):
            return
        _send_subscription(b, message.chat.id)

    @b.message_handler(func=lambda m: m.text == "📊 Реферальная система")
    def btn_referral(message):
        if not is_admin(message.from_user.id):
            return
        _send_referral(b, message.chat.id)

    # --- Callback: Пополнить баланс ---
    @b.callback_query_handler(func=lambda c: c.data == "top_up_balance")
    def cb_top_up(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("💳 СБП", callback_data="pay:sbp"))
        kb.add(tg_types.InlineKeyboardButton("💰 Иностранная карта", callback_data="pay:foreign_card"))
        kb.add(tg_types.InlineKeyboardButton("🏦 ЮMoney", callback_data="pay:yoomoney"))
        kb.add(tg_types.InlineKeyboardButton("🪙 USDT (TRC20)", callback_data="pay:usdt"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data="back_profile"))
        b.send_message(
            call.message.chat.id,
            "💳 *Выберите способ оплаты:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    @b.callback_query_handler(func=lambda c: c.data.startswith("pay:"))
    def cb_pay_method(call):
        if not is_admin(call.from_user.id):
            return
        method = call.data.split(":")[1]
        methods_map = {
            "sbp": "СБП",
            "foreign_card": "Иностранная карта",
            "yoomoney": "ЮMoney",
            "usdt": "USDT (TRC20)",
        }
        b.answer_callback_query(call.id, f"Выбран способ: {methods_map.get(method, method)}")
        b.send_message(
            call.message.chat.id,
            f"💳 Способ оплаты: *{methods_map.get(method, method)}*\n\n"
            "📝 Введите сумму пополнения (в RUB):",
            parse_mode="Markdown",
        )

    @b.callback_query_handler(func=lambda c: c.data == "back_profile")
    def cb_back_profile(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        _send_profile(b, call.message.chat.id)

    # --- Реферальная система callbacks ---
    @b.callback_query_handler(func=lambda c: c.data == "ref_update_link")
    def cb_ref_update(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "🔄 Ссылка обновлена!")

    @b.callback_query_handler(func=lambda c: c.data == "ref_notifications")
    def cb_ref_notifications(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "🔔 Уведомления переключены!")

    @b.callback_query_handler(func=lambda c: c.data == "ref_transfer")
    def cb_ref_transfer(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        b.send_message(
            call.message.chat.id,
            "💰 Введите сумму для перевода реферального баланса:",
        )


def _send_profile(b, chat_id: int):
    status = conn.playerok_status
    balance = status.get("balance")
    balance_str = f"{balance:.2f} RUB" if balance is not None else "—"

    text = (
        "🎉 *Личный кабинет*\n\n"
        f"🆔 ID: `{ADMIN_ID}`\n"
        f"⭐ Подписка: *Активна*\n\n"
        f"💰 *Финансы*\n"
        f"💎 Баланс: *{balance_str}*\n"
        f"📥 Пополнено: *0 RUB*\n\n"
        "⚡ _Управляйте своим аккаунтом легко и удобно!_"
    )

    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🤝 Реферальная система", callback_data="ref_menu"))
    kb.add(tg_types.InlineKeyboardButton("💳 Пополнить баланс", callback_data="top_up_balance"))
    kb.add(tg_types.InlineKeyboardButton("👑 Подписка", callback_data="sub_menu"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_subscription(b, chat_id: int):
    text = (
        "👑 *Подписка*\n\n"
        "⭐ Статус: *Активна*\n"
        "📅 Действует до: *Бессрочно*\n\n"
        "Подписка открывает доступ ко всем функциям бота, "
        "включая автопроцессы, статистику и расширенные модули.\n\n"
        "🎁 Пригласите друзей через реферальную систему, чтобы "
        "получить дополнительные бонусы и привилегии!"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🔄 Обновить ссылку", callback_data="ref_update_link"))
    kb.add(tg_types.InlineKeyboardButton("🔔 Уведомления", callback_data="ref_notifications"))
    kb.add(tg_types.InlineKeyboardButton("💰 Перевести баланс", callback_data="ref_transfer"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_referral(b, chat_id: int):
    text = (
        "📊 *Реферальная система*\n\n"
        "🔗 Ваша реферальная ссылка:\n"
        f"`https://t.me/your_bot?start=ref_{ADMIN_ID}`\n\n"
        "👥 Приглашённых: *0*\n"
        "💰 Заработано: *0 RUB*\n\n"
        "Приглашайте друзей и получайте бонусы!"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🔄 Обновить ссылку", callback_data="ref_update_link"))
    kb.add(tg_types.InlineKeyboardButton("🔔 Уведомления", callback_data="ref_notifications"))
    kb.add(tg_types.InlineKeyboardButton("💰 Перевести баланс", callback_data="ref_transfer"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
