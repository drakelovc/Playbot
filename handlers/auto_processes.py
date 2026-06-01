"""Автопроцессы аккаунта: Товары, Чаты, Финансы."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
from core.config import load_config, save_config


def register(b):

    # --- Меню автопроцессов ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_auto:"))
    def cb_auto_menu(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_auto_menu(b, call.message.chat.id, acc_name)

    # --- Товары ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("auto_items:"))
    def cb_auto_items(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_auto_items(b, call.message.chat.id, acc_name)

    # --- Чаты ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("auto_chats:"))
    def cb_auto_chats(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_auto_chats(b, call.message.chat.id, acc_name)

    # --- Финансы ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("auto_finance:"))
    def cb_auto_finance(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_auto_finance(b, call.message.chat.id, acc_name)

    # --- Тоглы автопроцессов ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("atoggle:"))
    def cb_auto_toggle(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":")
        key = parts[1]
        acc_name = parts[2] if len(parts) > 2 else ""
        cfg = load_config()
        cfg[key] = not cfg.get(key, False)
        save_config(cfg)
        state = "🟢 вкл" if cfg[key] else "🔴 выкл"
        b.answer_callback_query(call.id, f"{key}: {state}")

        if key in ("auto_bump", "auto_code", "auto_confirm", "auto_restore"):
            _send_auto_items(b, call.message.chat.id, acc_name)
        elif key.startswith("chat_"):
            _send_auto_chats(b, call.message.chat.id, acc_name)
        elif key.startswith("auto_withdraw"):
            _send_auto_finance(b, call.message.chat.id, acc_name)

    # --- Реквизиты вывода ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("withdraw_req:"))
    def cb_withdraw_requisites(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("💳 СБП", callback_data=f"set_req:sbp:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("💰 Иностранная карта", callback_data=f"set_req:foreign:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("🏦 ЮMoney", callback_data=f"set_req:yoomoney:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("🪙 USDT (TRC20)", callback_data=f"set_req:usdt:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"auto_finance:{acc_name}"))
        b.send_message(
            call.message.chat.id,
            "💳 *Выберите способ оплаты:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # --- Порог вывода ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("withdraw_threshold:"))
    def cb_withdraw_threshold(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        cfg = load_config()
        threshold = cfg.get("auto_withdraw_threshold", 1000)
        kb = tg_types.InlineKeyboardMarkup()
        kb.row(
            tg_types.InlineKeyboardButton("➖", callback_data=f"thresh_dec:{acc_name}"),
            tg_types.InlineKeyboardButton(f"{threshold} ₽", callback_data="noop"),
            tg_types.InlineKeyboardButton("➕", callback_data=f"thresh_inc:{acc_name}"),
        )
        kb.add(tg_types.InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"thresh_manual:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"auto_finance:{acc_name}"))
        b.send_message(
            call.message.chat.id,
            f"💰 *Порог автовывода:* {threshold} ₽\n\nИспользуйте кнопки для изменения:",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    @b.callback_query_handler(func=lambda c: c.data.startswith("thresh_dec:") or c.data.startswith("thresh_inc:"))
    def cb_thresh_change(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        cfg = load_config()
        current = cfg.get("auto_withdraw_threshold", 1000)
        if call.data.startswith("thresh_dec:"):
            cfg["auto_withdraw_threshold"] = max(100, current - 100)
        else:
            cfg["auto_withdraw_threshold"] = current + 100
        save_config(cfg)
        b.answer_callback_query(call.id, f"Порог: {cfg['auto_withdraw_threshold']} ₽")

    @b.callback_query_handler(func=lambda c: c.data.startswith("thresh_manual:"))
    def cb_thresh_manual(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id)
        msg = b.send_message(call.message.chat.id, "💰 Введите сумму порога (в ₽):")
        b.register_next_step_handler(msg, lambda m: _process_threshold(b, m))

    @b.callback_query_handler(func=lambda c: c.data == "noop")
    def cb_noop(call):
        b.answer_callback_query(call.id)


def _send_auto_menu(b, chat_id: int, acc_name: str):
    text = (
        f"⚙️ *Автопроцессы — {acc_name}*\n"
        "🚀 _Задачи, которые выполняются автоматически_"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.row(
        tg_types.InlineKeyboardButton("📦 Товары", callback_data=f"auto_items:{acc_name}"),
        tg_types.InlineKeyboardButton("💬 Чаты", callback_data=f"auto_chats:{acc_name}"),
        tg_types.InlineKeyboardButton("💰 Финансы", callback_data=f"auto_finance:{acc_name}"),
    )
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_auto_items(b, chat_id: int, acc_name: str):
    cfg = load_config()
    text = (
        f"📦 *Товары — Автопроцессы*\n\n"
        f"🔑 Новый заказ (автовыдача кода): {'🟢' if cfg.get('auto_code') else '🔴'}\n"
        f"✅ Новое подтверждение: {'🟢' if cfg.get('auto_confirm') else '🔴'}\n"
        f"🔄 Авто-восстановление: {'🟢' if cfg.get('auto_restore') else '🔴'}\n"
        f"🚀 Авто-поднятие: {'🟢' if cfg.get('auto_bump') else '🔴'}\n"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_code') else '🔴'} Новый заказ",
        callback_data=f"atoggle:auto_code:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_confirm') else '🔴'} Подтверждение",
        callback_data=f"atoggle:auto_confirm:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_restore') else '🔴'} Восстановление",
        callback_data=f"atoggle:auto_restore:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_bump') else '🔴'} Авто-поднятие",
        callback_data=f"atoggle:auto_bump:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_auto:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_auto_chats(b, chat_id: int, acc_name: str):
    cfg = load_config()
    text = (
        f"💬 *Чаты — Автопроцессы*\n\n"
        f"📩 Новые сообщения: {'🟢' if cfg.get('chat_new_messages') else '🔴'}\n"
        f"📢 Системные: {'🟢' if cfg.get('chat_notify_system') else '🔴'}\n"
        f"🛠 Поддержка: {'🟢' if cfg.get('chat_notify_support') else '🔴'}\n"
        f"🌐 Playerok.com: {'🟢' if cfg.get('chat_notify_playerok') else '🔴'}\n"
        f"📖 Авто-прочтение: {'🟢' if cfg.get('chat_auto_read') else '🔴'}\n"
        f"🔇 Игнор сообщений: {'🟢' if cfg.get('chat_ignore_messages') else '🔴'}\n"
        f"⌨️ Команды: {'🟢' if cfg.get('chat_commands_enabled') else '🔴'}\n"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('chat_new_messages') else '🔴'} Новые сообщения",
        callback_data=f"atoggle:chat_new_messages:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('chat_auto_read') else '🔴'} Авто-прочтение",
        callback_data=f"atoggle:chat_auto_read:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('chat_ignore_messages') else '🔴'} Игнор сообщений",
        callback_data=f"atoggle:chat_ignore_messages:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('chat_commands_enabled') else '🔴'} Команды",
        callback_data=f"atoggle:chat_commands_enabled:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_auto:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_auto_finance(b, chat_id: int, acc_name: str):
    cfg = load_config()
    threshold = cfg.get("auto_withdraw_threshold", 1000)
    method = cfg.get("auto_withdraw_method", "не указан")
    text = (
        f"💰 *Финансы — Автовывод*\n\n"
        f"⚡ Автовывод: {'🟢' if cfg.get('auto_withdraw_enabled') else '🔴'}\n"
        f"🔔 Уведомления: {'🟢' if cfg.get('auto_withdraw_notify') else '🔴'}\n"
        f"💳 Реквизиты: {method}\n"
        f"📊 Порог: {threshold} ₽\n"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_withdraw_enabled') else '🔴'} Автовывод",
        callback_data=f"atoggle:auto_withdraw_enabled:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton(
        f"{'🟢' if cfg.get('auto_withdraw_notify') else '🔴'} Уведомления",
        callback_data=f"atoggle:auto_withdraw_notify:{acc_name}",
    ))
    kb.add(tg_types.InlineKeyboardButton("💳 Реквизиты", callback_data=f"withdraw_req:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("📊 Порог", callback_data=f"withdraw_threshold:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_auto:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _process_threshold(b, message):
    try:
        val = int(message.text.strip())
        if val < 100:
            b.send_message(message.chat.id, "❌ Минимальный порог: 100 ₽")
            return
        cfg = load_config()
        cfg["auto_withdraw_threshold"] = val
        save_config(cfg)
        b.send_message(message.chat.id, f"✅ Порог установлен: {val} ₽")
    except ValueError:
        b.send_message(message.chat.id, "❌ Введите число.")
