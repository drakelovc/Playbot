"""Playerok event loop: обработка входящих событий."""
from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime

from playerokapi.enums import EventTypes, ItemDealStatuses, ItemStatuses

from core.config import load_config, save_config
from core.steam_guard import generate_code, get_account_names, seconds_until_code_change
import core.playerok_connection as conn

log = logging.getLogger("playerok_bot.event_loop")

_plugin_ctx = None
_plugins_mod = None
_bot = None
_admin_id: int = 0

_recently_restored_item_ids: dict[str, float] = {}


def set_context(bot, admin_id: int, plugin_ctx, plugins_mod):
    global _bot, _admin_id, _plugin_ctx, _plugins_mod
    _bot = bot
    _admin_id = admin_id
    _plugin_ctx = plugin_ctx
    _plugins_mod = plugins_mod


def _find_account_for_chat(chat_id: str) -> str | None:
    cfg = load_config()
    acc = cfg.get("account_map", {}).get(chat_id)
    if not acc:
        acc = cfg.get("default_account") or None
    return acc


def playerok_loop():
    if not conn.playerok_listener:
        return

    conn.playerok_running = True
    log.info("Playerok listener запущен")

    try:
        from telebot import types as tg_types

        for event in conn.playerok_listener.listen():
            if not conn.playerok_running:
                break

            conn.playerok_status["last_sync"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            cfg = load_config()

            if _plugin_ctx is not None and _plugins_mod is not None:
                try:
                    _plugins_mod.dispatch_event(event, _plugin_ctx)
                except Exception:
                    log.exception("plugin dispatch_event failed")

            # --- НОВОЕ СООБЩЕНИЕ ---
            if event.type is EventTypes.NEW_MESSAGE:
                if not event.message or not event.message.user:
                    continue
                if event.message.user.id == conn.playerok_acc.id:
                    continue

                cfg["stats"]["messages_received"] += 1
                save_config(cfg)

                msg_text = (event.message.text or "").strip()
                msg_lower = msg_text.lower()
                chat_id = event.chat.id
                buyer_name = getattr(event.message.user, 'username', None) or "Покупатель"

                log.info("[MSG] %s: %s", buyer_name, msg_text)

                if cfg.get("notify_new_message") and _bot:
                    try:
                        kb = tg_types.InlineKeyboardMarkup()
                        kb.row(
                            tg_types.InlineKeyboardButton("💬 Ответить", callback_data=f"reply:{chat_id}"),
                            tg_types.InlineKeyboardButton("🔑 Отправить код", callback_data=f"pickcode:{chat_id}"),
                        )
                        quick = cfg.get("quick_replies") or []
                        for i, tpl in enumerate(quick[:6]):
                            short = (tpl[:18] + "…") if len(tpl) > 19 else tpl
                            kb.add(tg_types.InlineKeyboardButton(
                                f"⚡ {short}", callback_data=f"qr:{chat_id}:{i}",
                            ))
                        _bot.send_message(
                            _admin_id,
                            f"💬 *Новое сообщение*\n\n👤 {buyer_name}\n📝 {msg_text}\n💬 Чат: `{chat_id}`",
                            parse_mode="Markdown", reply_markup=kb,
                        )
                    except Exception:
                        pass

                # !code fallback
                if msg_lower.startswith("!code"):
                    parts = msg_text.split(maxsplit=1)
                    if len(parts) >= 2:
                        requested_name = parts[1].strip()
                        code = generate_code(requested_name)
                        if code:
                            remaining = seconds_until_code_change()
                            try:
                                conn.playerok_acc.send_message(
                                    chat_id=chat_id,
                                    text=f"🔐 Steam Guard код для {requested_name}:\n{code}\n⏱ Действует ещё {remaining} сек."
                                )
                                cfg = load_config()
                                cfg["stats"]["codes_sent"] += 1
                                cfg["history"].append({
                                    "type": "code", "account": requested_name,
                                    "buyer": buyer_name, "chat_id": chat_id,
                                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                })
                                cfg["history"] = cfg["history"][-200:]
                                save_config(cfg)
                            except Exception:
                                pass
                    else:
                        acc_name = _find_account_for_chat(chat_id)
                        if acc_name:
                            code = generate_code(acc_name)
                            if code:
                                remaining = seconds_until_code_change()
                                try:
                                    conn.playerok_acc.send_message(
                                        chat_id=chat_id,
                                        text=f"🔐 Steam Guard код:\n{code}\n⏱ Действует ещё {remaining} сек."
                                    )
                                except Exception:
                                    pass
                    continue

                # Автоответчик
                if cfg.get("auto_responder"):
                    responses = cfg.get("auto_responses", {})
                    for trigger, response in responses.items():
                        if trigger.lower() in msg_lower:
                            try:
                                conn.playerok_acc.send_message(chat_id=chat_id, text=response)
                            except Exception:
                                pass
                            break

                # Приветствие
                if cfg.get("auto_greeting"):
                    greeted = cfg.get("greeted_chats", [])
                    if chat_id not in greeted:
                        greeting = cfg.get("greeting_text", "")
                        if greeting:
                            try:
                                conn.playerok_acc.send_message(chat_id=chat_id, text=greeting)
                                cfg["greeted_chats"].append(chat_id)
                                cfg["greeted_chats"] = cfg["greeted_chats"][-500:]
                                save_config(cfg)
                            except Exception:
                                pass

            # --- ТОВАР ОПЛАЧЕН ---
            elif event.type is EventTypes.ITEM_PAID:
                deal = event.deal
                buyer = getattr(deal.buyer, 'username', None) or "Покупатель" if hasattr(deal, 'buyer') and deal.buyer else "?"
                item_name = getattr(deal.item, 'name', "Товар") if hasattr(deal, 'item') and deal.item else "Товар"
                price = getattr(deal, 'price', 0)
                price_val = price / 100 if isinstance(price, int) and price > 100 else price

                cfg = load_config()
                cfg["stats"]["deals_total"] += 1
                cfg["stats"]["revenue"] += float(price_val or 0)
                cfg["history"].append({
                    "type": "deal", "deal_id": deal.id, "item": item_name,
                    "buyer": buyer, "price": price_val,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                cfg["history"] = cfg["history"][-200:]
                save_config(cfg)

                if cfg.get("notify_new_deal") and _bot:
                    try:
                        kb = tg_types.InlineKeyboardMarkup()
                        kb.row(
                            tg_types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_deal:{deal.id}"),
                            tg_types.InlineKeyboardButton("↩️ Возврат", callback_data=f"rollback_deal:{deal.id}"),
                        )
                        _bot.send_message(
                            _admin_id,
                            f"💰 *Новая оплата!*\n\n🛒 {item_name}\n👤 {buyer}\n💵 {price_val:.2f} ₽\n🆔 `{deal.id}`",
                            parse_mode="Markdown", reply_markup=kb,
                        )
                    except Exception:
                        pass

                if cfg.get("auto_confirm"):
                    time.sleep(3)
                    try:
                        conn.playerok_acc.update_deal(deal.id, ItemDealStatuses.SENT)
                        cfg = load_config()
                        cfg["stats"]["deals_confirmed"] += 1
                        save_config(cfg)
                    except Exception:
                        pass

            # --- СДЕЛКА ПОДТВЕРЖДЕНА ---
            elif event.type is EventTypes.DEAL_CONFIRMED or (
                hasattr(EventTypes, "DEAL_CONFIRMED_AUTOMATICALLY")
                and event.type is EventTypes.DEAL_CONFIRMED_AUTOMATICALLY
            ):
                deal = event.deal
                if cfg.get("notify_deal_confirmed") and _bot:
                    try:
                        _bot.send_message(_admin_id, f"✅ *Сделка подтверждена*\n🆔 `{deal.id}`", parse_mode="Markdown")
                    except Exception:
                        pass
                after_text = cfg.get("after_confirm_text", "")
                if after_text:
                    try:
                        conn.playerok_acc.send_message(chat_id=event.chat.id, text=after_text)
                    except Exception:
                        pass

                if cfg.get("auto_restore"):
                    try:
                        item = deal.item
                        item_id = item.id
                        item_price = getattr(item, "price", 0)
                        statuses = conn.playerok_acc.get_item_priority_statuses(item_id, item_price)
                        free_status = next((s for s in statuses if s.price == 0), None)
                        if free_status:
                            conn.playerok_acc.publish_item(item_id, free_status.id)
                            _recently_restored_item_ids[str(item_id)] = time.time()
                    except Exception:
                        pass

            # --- ПРОБЛЕМА ---
            elif event.type is EventTypes.DEAL_HAS_PROBLEM:
                if cfg.get("notify_deal_problem") and _bot:
                    try:
                        _bot.send_message(
                            _admin_id,
                            f"⚠️ *Проблема со сделкой!*\n🆔 `{event.deal.id}`\nПроверь Playerok!",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

    except Exception as exc:
        conn.playerok_running = False
        conn.playerok_status["error"] = str(exc)
        conn.playerok_status["connected"] = False
        log.error("Playerok listener упал: %s\n%s", exc, traceback.format_exc())
        if _bot:
            try:
                _bot.send_message(_admin_id, f"❌ *Playerok listener упал!*\n`{exc}`", parse_mode="Markdown")
            except Exception:
                pass
        time.sleep(30)
        try:
            if conn.init_playerok(load_config()):
                start_playerok_thread()
        except Exception:
            pass


def start_playerok_thread():
    import threading
    if hasattr(start_playerok_thread, '_thread') and start_playerok_thread._thread and start_playerok_thread._thread.is_alive():
        conn.playerok_running = False
        time.sleep(2)
    t = threading.Thread(target=playerok_loop, daemon=True)
    start_playerok_thread._thread = t
    t.start()
