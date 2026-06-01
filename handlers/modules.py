"""Модули (плагины) аккаунта: включение/выключение."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
from core.config import load_config, save_config
import plugins as _plugins


def register(b):

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_modules:"))
    def cb_modules_menu(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_modules(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("mod_info:"))
    def cb_mod_info(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":")
        plugin_id = parts[1]
        acc_name = parts[2] if len(parts) > 2 else ""
        b.answer_callback_query(call.id)
        _send_plugin_info(b, call.message.chat.id, plugin_id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("mod_toggle:"))
    def cb_mod_toggle(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":")
        plugin_id = parts[1]
        acc_name = parts[2] if len(parts) > 2 else ""
        cfg = load_config()
        is_on = _plugins.is_enabled(plugin_id, cfg)
        _plugins.set_enabled(plugin_id, cfg, not is_on)
        save_config(cfg)
        state = "🔴 выключен" if is_on else "🟢 включён"
        b.answer_callback_query(call.id, f"Модуль {state}")
        _send_modules(b, call.message.chat.id, acc_name)


def _send_modules(b, chat_id: int, acc_name: str):
    cfg = load_config()
    all_p = _plugins.all_plugins()
    text = f"🧩 *Модули*\n\nИспользуйте кнопки для управления:"
    kb = tg_types.InlineKeyboardMarkup()
    for p in all_p:
        enabled = _plugins.is_enabled(p.id, cfg)
        icon = "🟢" if enabled else "🔴"
        kb.add(tg_types.InlineKeyboardButton(
            f"{icon} {p.icon} {p.name}",
            callback_data=f"mod_info:{p.id}:{acc_name}",
        ))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_plugin_info(b, chat_id: int, plugin_id: str, acc_name: str):
    p = _plugins.get(plugin_id)
    if not p:
        b.send_message(chat_id, f"❌ Модуль `{plugin_id}` не найден.", parse_mode="Markdown")
        return
    cfg = load_config()
    enabled = _plugins.is_enabled(plugin_id, cfg)
    text = (
        f"{p.icon} *{p.name}*\n"
        f"{'🟢 Включён' if enabled else '🔴 Выключен'}\n\n"
        f"{p.description}\n\n"
        f"📋 *Инструкция:*\n{p.instruction}"
    )
    kb = tg_types.InlineKeyboardMarkup()
    toggle_text = "🔴 Выключить" if enabled else "🟢 Включить"
    kb.add(tg_types.InlineKeyboardButton(toggle_text, callback_data=f"mod_toggle:{plugin_id}:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад к модулям", callback_data=f"acc_modules:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
