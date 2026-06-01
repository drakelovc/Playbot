"""Действия: ответ на сообщение, отправка кода, загрузка maFile, подтверждение/возврат сделки."""
from __future__ import annotations

import io
import json
import os
import zipfile

from telebot import types as tg_types

from core.bot_instance import is_admin
from core.config import load_config, save_config, ACCOUNTS_FOLDER
from core.steam_guard import generate_code, get_account_names, seconds_until_code_change
import core.playerok_connection as conn
import plugins as _plugins


def register(b):

    # --- Ответить в чат ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("reply:"))
    def cb_reply(call):
        if not is_admin(call.from_user.id):
            return
        chat_id = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        msg = b.send_message(call.message.chat.id, "💬 Введите ответ:")
        b.register_next_step_handler(msg, lambda m: _process_reply(b, m, chat_id))

    # --- Отправить код (выбор аккаунта) ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("pickcode:"))
    def cb_pickcode(call):
        if not is_admin(call.from_user.id):
            return
        chat_id = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id)
        names = get_account_names()
        if not names:
            b.send_message(call.message.chat.id, "❌ Нет загруженных аккаунтов.")
            return
        kb = tg_types.InlineKeyboardMarkup()
        for name in names[:15]:
            kb.add(tg_types.InlineKeyboardButton(
                f"🎮 {name}", callback_data=f"sendcode:{chat_id}:{name}",
            ))
        kb.add(tg_types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_action"))
        b.send_message(call.message.chat.id, "🔑 Выберите аккаунт:", reply_markup=kb)

    @b.callback_query_handler(func=lambda c: c.data.startswith("sendcode:"))
    def cb_sendcode(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":", 2)
        chat_id = parts[1]
        acc_name = parts[2]
        b.answer_callback_query(call.id, "🔐 Генерирую код...")
        code = generate_code(acc_name)
        if code:
            remaining = seconds_until_code_change()
            try:
                if conn.playerok_acc:
                    conn.playerok_acc.send_message(
                        chat_id=chat_id,
                        text=f"🔐 Steam Guard код для {acc_name}:\n{code}\n⏱ Действует ещё {remaining} сек."
                    )
                b.send_message(
                    call.message.chat.id,
                    f"✅ Код отправлен!\n🎮 `{acc_name}`\n🔑 `{code}`\n⏱ {remaining} сек.",
                    parse_mode="Markdown",
                )
            except Exception as exc:
                b.send_message(call.message.chat.id, f"❌ Ошибка: {exc}")
        else:
            b.send_message(call.message.chat.id, f"❌ Аккаунт «{acc_name}» не найден или нет shared_secret.")

    # --- Быстрые ответы ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("qr:"))
    def cb_quick_reply(call):
        if not is_admin(call.from_user.id):
            return
        parts = call.data.split(":", 2)
        chat_id = parts[1]
        idx = int(parts[2])
        cfg = load_config()
        templates = cfg.get("quick_replies") or []
        if idx < len(templates):
            text = templates[idx]
            try:
                if conn.playerok_acc:
                    conn.playerok_acc.send_message(chat_id=chat_id, text=text)
                b.answer_callback_query(call.id, "✅ Отправлено!")
            except Exception as exc:
                b.answer_callback_query(call.id, f"❌ {exc}")
        else:
            b.answer_callback_query(call.id, "❌ Шаблон не найден")

    # --- Подтвердить / Возврат сделки ---
    @b.callback_query_handler(func=lambda c: c.data.startswith("confirm_deal:"))
    def cb_confirm_deal(call):
        if not is_admin(call.from_user.id):
            return
        deal_id = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "✅ Подтверждаю...")
        try:
            from playerokapi.enums import ItemDealStatuses
            if conn.playerok_acc:
                conn.playerok_acc.update_deal(deal_id, ItemDealStatuses.SENT)
                b.send_message(call.message.chat.id, f"✅ Сделка `{deal_id}` подтверждена.", parse_mode="Markdown")
        except Exception as exc:
            b.send_message(call.message.chat.id, f"❌ Ошибка: {exc}")

    @b.callback_query_handler(func=lambda c: c.data.startswith("rollback_deal:"))
    def cb_rollback_deal(call):
        if not is_admin(call.from_user.id):
            return
        deal_id = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "↩️ Возврат...")
        b.send_message(call.message.chat.id, f"↩️ Возврат сделки `{deal_id}` (необходимо обработать вручную на Playerok).", parse_mode="Markdown")

    @b.callback_query_handler(func=lambda c: c.data == "cancel_action")
    def cb_cancel(call):
        b.answer_callback_query(call.id, "Отменено")

    # --- Загрузка .maFile и .zip ---
    @b.message_handler(content_types=["document"])
    def handle_document(message):
        if not is_admin(message.from_user.id):
            return
        doc = message.document
        if not doc or not doc.file_name:
            return

        cfg = load_config()
        if not _plugins.is_enabled("autosteamoffline", cfg) and not _plugins.is_enabled("autosteamrental", cfg):
            b.send_message(
                message.chat.id,
                "⚠️ Плагин *Steam Guard коды* выключен — загрузка .maFile отключена.",
                parse_mode="Markdown",
            )
            return

        if doc.file_name.endswith(".maFile"):
            _handle_mafile(b, message, doc)
        elif doc.file_name.endswith(".zip"):
            _handle_zip(b, message, doc)


def _process_reply(b, message, chat_id: str):
    text = (message.text or "").strip()
    if not text:
        b.send_message(message.chat.id, "❌ Пустой текст.")
        return
    try:
        if conn.playerok_acc:
            conn.playerok_acc.send_message(chat_id=chat_id, text=text)
            b.send_message(message.chat.id, "✅ Сообщение отправлено!")
        else:
            b.send_message(message.chat.id, "❌ Playerok не подключён.")
    except Exception as exc:
        b.send_message(message.chat.id, f"❌ Ошибка: {exc}")


def _handle_mafile(b, message, doc):
    file_info = b.get_file(doc.file_id)
    data = b.download_file(file_info.file_path)
    try:
        ma_data = json.loads(data.decode("utf-8"))
        if not ma_data.get("shared_secret"):
            b.send_message(message.chat.id, "❌ В файле нет `shared_secret`.")
            return
        name = ma_data.get("account_name", doc.file_name.replace(".maFile", ""))
        os.makedirs(ACCOUNTS_FOLDER, exist_ok=True)
        path = os.path.join(ACCOUNTS_FOLDER, doc.file_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ma_data, f, indent=2, ensure_ascii=False)
        b.send_message(message.chat.id, f"✅ Аккаунт `{name}` добавлен!", parse_mode="Markdown")
    except Exception as exc:
        b.send_message(message.chat.id, f"❌ Ошибка: {exc}")


def _handle_zip(b, message, doc):
    file_info = b.get_file(doc.file_id)
    data = b.download_file(file_info.file_path)
    try:
        added = 0
        skipped = 0
        names_added = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for zi in zf.infolist():
                if zi.filename.endswith(".maFile") and not zi.is_dir():
                    try:
                        raw = zf.read(zi.filename)
                        ma = json.loads(raw.decode("utf-8"))
                        if not ma.get("shared_secret"):
                            skipped += 1
                            continue
                        fname = os.path.basename(zi.filename)
                        name = ma.get("account_name", fname.replace(".maFile", ""))
                        os.makedirs(ACCOUNTS_FOLDER, exist_ok=True)
                        path = os.path.join(ACCOUNTS_FOLDER, fname)
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(ma, f, indent=2, ensure_ascii=False)
                        added += 1
                        names_added.append(name)
                    except Exception:
                        skipped += 1
        text = f"✅ Импорт: добавлено {added}"
        if names_added:
            text += "\n• " + "\n• ".join(names_added)
        if skipped:
            text += f"\n⚠️ Пропущено: {skipped}"
        b.send_message(message.chat.id, text)
    except Exception as exc:
        b.send_message(message.chat.id, f"❌ Ошибка: {exc}")
