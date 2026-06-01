"""Настройки аккаунта: прокси, куки, чёрный список, получить токен, удалить аккаунт."""
from __future__ import annotations

from telebot import types as tg_types

from core.bot_instance import is_admin
from core.config import load_config, save_config
from core.helpers import normalize_proxy, check_proxy
import core.playerok_connection as conn


def register(b):

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_settings:"))
    def cb_settings_menu(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_settings(b, call.message.chat.id, acc_name)

    # --- Прокси ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("set_proxy:"))
    def cb_proxy(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        _send_proxy_info(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("proxy_check:"))
    def cb_proxy_check(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "🔍 Проверяю прокси...")
        cfg = load_config()
        proxy = cfg.get("playerok_proxy", "")
        if not proxy:
            b.send_message(call.message.chat.id, "❌ Прокси не установлен.")
            return
        waiting = b.send_message(call.message.chat.id, f"🔍 Проверяю `{proxy}`...", parse_mode="Markdown")
        result = check_proxy(proxy)
        if result["ok"]:
            ip = result.get("ip", "?")
            ms = result.get("ms", "?")
            b.send_message(
                call.message.chat.id,
                f"✅ *Прокси работает!*\n\n"
                f"🌐 IP: `{ip}`\n"
                f"⚡ Скорость: {ms} мс",
                parse_mode="Markdown",
            )
        else:
            error = result.get("error", "Неизвестная ошибка")
            b.send_message(
                call.message.chat.id,
                f"❌ *Прокси не работает!*\n\nОшибка: {error}",
                parse_mode="Markdown",
            )

    @b.callback_query_handler(func=lambda c: c.data.startswith("proxy_change:"))
    def cb_proxy_change(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        msg = b.send_message(
            call.message.chat.id,
            "🌐 Введите прокси в формате:\n"
            "`http://user:pass@ip:port`\n\n"
            "Или отправьте `0` чтобы убрать прокси.",
            parse_mode="Markdown",
        )
        b.register_next_step_handler(msg, lambda m: _process_proxy(b, m))

    # --- Добавить куки ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("set_cookies:"))
    def cb_cookies(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_settings:{acc_name}"))
        msg = b.send_message(
            call.message.chat.id,
            "🍪 *Добавить куки*\n\n"
            "📖 Подробнее: https://telegra.ph/Kak-najti-parametr-cf-clearance-v-cookie-06-17\n\n"
            "🔑 Введите значение `user_agent`:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_user_agent(b, m, acc_name))

    # --- Чёрный список ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("set_blacklist:"))
    def cb_blacklist(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"bl_block:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("✅ Разблокировать", callback_data=f"bl_unblock:{acc_name}"))
        kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_settings:{acc_name}"))
        b.send_message(
            call.message.chat.id,
            "🚫 *Чёрный список:*",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    @b.callback_query_handler(func=lambda c: c.data.startswith("bl_block:"))
    def cb_bl_block(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        msg = b.send_message(call.message.chat.id, "🚫 Введите имя пользователя для блокировки:")
        b.register_next_step_handler(msg, lambda m: _process_block(b, m))

    @b.callback_query_handler(func=lambda c: c.data.startswith("bl_unblock:"))
    def cb_bl_unblock(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        cfg = load_config()
        bl = cfg.get("blacklist", [])
        if bl:
            msg = b.send_message(
                call.message.chat.id,
                f"✅ В чёрном списке:\n" + "\n".join(f"• {u}" for u in bl) +
                "\n\nВведите имя для разблокировки:"
            )
            b.register_next_step_handler(msg, lambda m: _process_unblock(b, m))
        else:
            b.send_message(call.message.chat.id, "✅ Чёрный список пуст.")

    # --- Получить токен ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("set_get_token:"))
    def cb_get_token(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_settings:{acc_name}"))
        msg = b.send_message(
            call.message.chat.id,
            "📧 Чтобы получить токен, введите email от своего аккаунта:",
            reply_markup=kb,
        )
        b.register_next_step_handler(msg, lambda m: _process_get_token(b, m))

    # --- Удалить аккаунт ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("set_delete_acc:"))
    def cb_delete_acc(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        kb = tg_types.InlineKeyboardMarkup()
        kb.row(
            tg_types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete:{acc_name}"),
            tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_settings:{acc_name}"),
        )
        b.send_message(
            call.message.chat.id,
            f"⚠️ Вы уверены, что хотите удалить аккаунт *{acc_name}*?",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    @b.callback_query_handler(func=lambda c: c.data.startswith("confirm_delete:"))
    def cb_confirm_delete(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "🗑 Аккаунт удалён!")
        cfg = load_config()
        cfg["playerok_cookies"] = ""
        cfg["playerok_user_agent"] = ""
        cfg["playerok_proxy"] = ""
        save_config(cfg)
        conn.playerok_status["connected"] = False
        conn.playerok_acc = None
        b.send_message(call.message.chat.id, f"✅ Аккаунт *{acc_name}* удалён.", parse_mode="Markdown")

    # --- Обновить данные ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_refresh:"))
    def cb_refresh(call):
        if not is_admin(call.from_user.id):
            return
        b.answer_callback_query(call.id, "🔄 Обновляю данные...")
        cfg = load_config()
        if conn.init_playerok(cfg):
            b.send_message(call.message.chat.id, "✅ Данные обновлены!")
        else:
            b.send_message(call.message.chat.id, f"❌ Ошибка: {conn.playerok_status.get('error')}")


def _send_settings(b, chat_id: int, acc_name: str):
    text = f"⚙️ *Настройки аккаунта*"
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("🌐 Прокси", callback_data=f"set_proxy:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🍪 Добавить куки", callback_data=f"set_cookies:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🚫 Чёрный список", callback_data=f"set_blacklist:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🔑 Получить токен", callback_data=f"set_get_token:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("🗑 Удалить аккаунт", callback_data=f"set_delete_acc:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_proxy_info(b, chat_id: int, acc_name: str):
    cfg = load_config()
    proxy = cfg.get("playerok_proxy", "")
    if proxy:
        clean = proxy.replace("https://", "").replace("http://", "")
        parts = clean.split("@")
        if len(parts) == 2:
            creds = parts[0].split(":")
            host_parts = parts[1].split(":")
            login = creds[0] if creds else "?"
            password = creds[1] if len(creds) > 1 else "?"
            ip = host_parts[0] if host_parts else "?"
            port = host_parts[1] if len(host_parts) > 1 else "?"
        else:
            login = password = "?"
            hp = clean.split(":")
            ip = hp[0] if hp else "?"
            port = hp[1] if len(hp) > 1 else "?"

        text = (
            f"🌐 *Прокси:*\n\n"
            f"🔧 Тип: *http*\n"
            f"👤 Логин: `{login}`\n"
            f"🔑 Пароль: `{password}`\n"
            f"📡 IP адрес: `{ip}`\n"
            f"🔌 Порт: `{port}`\n"
            f"🔗 Полная ссылка:\n`{proxy}`"
        )
    else:
        text = "🌐 *Прокси:* не установлен"

    kb = tg_types.InlineKeyboardMarkup()
    kb.row(
        tg_types.InlineKeyboardButton("🔍 Проверить", callback_data=f"proxy_check:{acc_name}"),
        tg_types.InlineKeyboardButton("✏️ Изменить", callback_data=f"proxy_change:{acc_name}"),
    )
    kb.add(tg_types.InlineKeyboardButton("🛒 Купить", url="https://proxy6.net/"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_settings:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _process_proxy(b, message):
    text = (message.text or "").strip()
    cfg = load_config()
    if text == "0":
        cfg["playerok_proxy"] = ""
        save_config(cfg)
        b.send_message(message.chat.id, "✅ Прокси убран.")
        return

    if not text:
        return

    normalized = normalize_proxy(text)
    cfg["playerok_proxy"] = normalized
    save_config(cfg)

    if normalized != text:
        b.send_message(message.chat.id, f"🔄 Формат автоисправлен:\n`{normalized}`", parse_mode="Markdown")

    waiting = b.send_message(message.chat.id, "🔍 Проверяю прокси...")
    result = check_proxy(normalized)

    if result["ok"]:
        ip = result.get("ip", "?")
        ms = result.get("ms", "?")
        b.send_message(
            message.chat.id,
            f"✅ *Прокси работает!*\n\n"
            f"🌐 IP: `{ip}`\n"
            f"⚡ Скорость: {ms} мс",
            parse_mode="Markdown",
        )
    else:
        error = result.get("error", "Неизвестная ошибка")
        b.send_message(
            message.chat.id,
            f"❌ *Прокси не работает!*\n\n"
            f"Ошибка: {error}\n\n"
            "Прокси сохранён, но рекомендуется заменить.",
            parse_mode="Markdown",
        )


def _process_user_agent(b, message, acc_name: str):
    ua = (message.text or "").strip()
    if not ua:
        b.send_message(message.chat.id, "❌ Пустое значение.")
        return
    cfg = load_config()
    cfg["playerok_user_agent"] = ua
    save_config(cfg)
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data=f"acc_settings:{acc_name}"))
    msg = b.send_message(
        message.chat.id,
        f"✅ User-Agent сохранён.\n\n🍪 Теперь введите значение `cf_clearance` cookie:",
        reply_markup=kb,
    )
    b.register_next_step_handler(msg, lambda m: _process_cf_clearance(b, m))


def _process_cf_clearance(b, message):
    cookie = (message.text or "").strip()
    if not cookie:
        b.send_message(message.chat.id, "❌ Пустое значение.")
        return
    cfg = load_config()
    existing = cfg.get("playerok_cookies", "")
    if "cf_clearance=" in existing:
        import re
        existing = re.sub(r'cf_clearance=[^;]*;?', '', existing).strip('; ')
    if existing:
        cfg["playerok_cookies"] = f"{existing}; cf_clearance={cookie}"
    else:
        cfg["playerok_cookies"] = f"cf_clearance={cookie}"
    save_config(cfg)
    b.send_message(message.chat.id, "✅ Куки сохранены!")


def _process_block(b, message):
    username = (message.text or "").strip()
    if not username:
        b.send_message(message.chat.id, "❌ Пустое имя.")
        return
    cfg = load_config()
    bl = cfg.get("blacklist", [])
    if username not in bl:
        bl.append(username)
        cfg["blacklist"] = bl
        save_config(cfg)
    b.send_message(message.chat.id, f"🚫 Пользователь `{username}` заблокирован.", parse_mode="Markdown")


def _process_unblock(b, message):
    username = (message.text or "").strip()
    cfg = load_config()
    bl = cfg.get("blacklist", [])
    if username in bl:
        bl.remove(username)
        cfg["blacklist"] = bl
        save_config(cfg)
        b.send_message(message.chat.id, f"✅ Пользователь `{username}` разблокирован.", parse_mode="Markdown")
    else:
        b.send_message(message.chat.id, f"❌ `{username}` нет в чёрном списке.", parse_mode="Markdown")


def _process_get_token(b, message):
    email = (message.text or "").strip()
    if not email or "@" not in email:
        b.send_message(message.chat.id, "❌ Некорректный email.")
        return
    b.send_message(
        message.chat.id,
        f"📧 Запрос токена отправлен на *{email}*.",
        parse_mode="Markdown",
    )
