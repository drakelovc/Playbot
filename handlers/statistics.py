"""Статистика аккаунта: продажи за период, экспорт XLSX/TXT."""
from __future__ import annotations

import io
import json
from datetime import datetime

from telebot import types as tg_types

from core.bot_instance import is_admin
from core.config import load_config
from core.helpers import filter_history_by_period
import core.playerok_connection as conn


def register(b):

    @b.callback_query_handler(func=lambda c: c.data.startswith("acc_stats:"))
    def cb_stats_menu(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "⏳ Подождите, идёт обработка...")
        _send_stats(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("stats_total:"))
    def cb_stats_total(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "⏳ Подождите, идёт обработка...")
        _send_stats_total(b, call.message.chat.id, acc_name)

    @b.callback_query_handler(func=lambda c: c.data.startswith("stats_export_xlsx:"))
    def cb_export_xlsx(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "📊 Генерирую XLSX...")
        _export_stats(b, call.message.chat.id, acc_name, "xlsx")

    @b.callback_query_handler(func=lambda c: c.data.startswith("stats_export_txt:"))
    def cb_export_txt(call):
        if not is_admin(call.from_user.id):
            return
        acc_name = call.data.split(":", 1)[1]
        b.answer_callback_query(call.id, "📄 Генерирую TXT...")
        _export_stats(b, call.message.chat.id, acc_name, "txt")


def _send_stats(b, chat_id: int, acc_name: str):
    cfg = load_config()
    stats = cfg.get("stats", {})
    history = cfg.get("history", [])
    week_history = filter_history_by_period(history, "7d")

    deals_week = sum(1 for h in week_history if h.get("type") == "deal")
    revenue_week = sum(h.get("price", 0) for h in week_history if h.get("type") == "deal")

    text = (
        f"📊 *Статистика продаж за 7 дней*\n"
        f"🚀 _Данные собираются при включённых функциях \"Новые заказы\" / \"Авто-поднятия\"_\n"
        f"⚠️ _Комиссия уже вычтена из суммы продаж: Товар - Комиссия - 6% - Премиум_\n\n"
        f"📅 Выберите дату, с которой хотите посмотреть статистику"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.add(tg_types.InlineKeyboardButton("📊 Итого", callback_data=f"stats_total:{acc_name}"))
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"select_acc:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _send_stats_total(b, chat_id: int, acc_name: str):
    cfg = load_config()
    history = cfg.get("history", [])
    week_history = filter_history_by_period(history, "7d")

    deals_week = sum(1 for h in week_history if h.get("type") == "deal")
    revenue_week = sum(h.get("price", 0) for h in week_history if h.get("type") == "deal")
    bumps_week = sum(1 for h in week_history if h.get("type") == "bump")

    text = (
        f"📊 *Итого за неделю:*\n"
        f"🛒 Продаж: *{deals_week}* ({revenue_week:.1f} ₽)\n"
        f"🚀 Поднятий: *{bumps_week}* ({0.0:.1f} ₽)\n"
        f"💰 Прибыль: *{revenue_week:.1f} ₽*\n\n"
        "💡 _Чтобы получить полную статистику по каждому товару — "
        "экспортируйте файл в удобном формате_"
    )
    kb = tg_types.InlineKeyboardMarkup()
    kb.row(
        tg_types.InlineKeyboardButton("📊 XLSX", callback_data=f"stats_export_xlsx:{acc_name}"),
        tg_types.InlineKeyboardButton("📄 TXT", callback_data=f"stats_export_txt:{acc_name}"),
    )
    kb.add(tg_types.InlineKeyboardButton("↩️ Назад", callback_data=f"acc_stats:{acc_name}"))
    b.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)


def _export_stats(b, chat_id: int, acc_name: str, fmt: str):
    cfg = load_config()
    history = cfg.get("history", [])
    week_history = filter_history_by_period(history, "7d")
    deals = [h for h in week_history if h.get("type") == "deal"]

    if fmt == "txt":
        lines = ["Статистика продаж за 7 дней", "=" * 40, ""]
        for d in deals:
            lines.append(
                f"{d.get('time', '?')} | {d.get('item', '?')} | "
                f"{d.get('buyer', '?')} | {d.get('price', 0):.2f} ₽"
            )
        if not deals:
            lines.append("Нет продаж за период.")
        total = sum(d.get("price", 0) for d in deals)
        lines.extend(["", f"Итого: {len(deals)} продаж, {total:.2f} ₽"])
        content = "\n".join(lines)
        doc = io.BytesIO(content.encode("utf-8"))
        doc.name = "statistics.txt"
        b.send_document(chat_id, doc, caption="📄 Статистика за 7 дней")

    elif fmt == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Статистика"
            ws.append(["Дата", "Товар", "Покупатель", "Цена"])
            for d in deals:
                ws.append([
                    d.get("time", ""),
                    d.get("item", ""),
                    d.get("buyer", ""),
                    d.get("price", 0),
                ])
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            buf.name = "statistics.xlsx"
            b.send_document(chat_id, buf, caption="📊 Статистика за 7 дней")
        except ImportError:
            content = json.dumps(deals, ensure_ascii=False, indent=2)
            doc = io.BytesIO(content.encode("utf-8"))
            doc.name = "statistics.json"
            b.send_document(chat_id, doc, caption="📊 Статистика (XLSX недоступен, отправляю JSON)")
