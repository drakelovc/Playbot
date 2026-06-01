"""Реестр плагинов для playerok_bot.

Плагины — самостоятельные модули, расширяющие функциональность бота, которые
можно включать/выключать через меню «🧩 Плагины» в Telegram.

Каждый плагин состоит из:
  * `PLUGIN` (`Plugin` — метаданные: id, имя, описание, инструкция);
  * опционально — объекта-обработчика, который умеет:
      - `register_telegram(bot, admin_id)` — навешать свои Telegram-команды и
        callback-хендлеры;
      - `on_event(event, ctx)` — обработать событие из PlayerokAPI
        (`NEW_MESSAGE`, `ITEM_PAID`, `DEAL_CONFIRMED`, ...);
      - `start_background(ctx)` — запустить фоновую задачу (чекер истечения
        аренды, напоминания и т. п.);
      - `setup(ctx)` — однократная инициализация при подключении к Playerok.

`ctx` — это `PluginContext` со ссылками на playerok-аккаунт, Telegram-бот,
конфиг и логгер. Через него плагины общаются с ядром бота, не зная о его
внутренностях.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    icon: str
    description: str  # короткое описание (1-2 строки)
    instruction: str  # markdown-инструкция (что делает + как настроить)
    default_enabled: bool = True
    keywords: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class PluginContext:
    """Контекст, который ядро бота передаёт плагину."""
    playerok_acc: Any  # playerokapi.Account | None
    bot: Any  # telebot.TeleBot
    admin_id: int
    get_config: Callable[[], dict]
    save_config: Callable[[dict], None]
    log: logging.Logger


class PluginHandler(Protocol):
    """Опциональный интерфейс обработчика плагина.

    Все методы опциональны — если их нет, ядро бота просто их не вызывает.
    """

    def setup(self, ctx: PluginContext) -> None: ...
    def register_telegram(self, ctx: PluginContext) -> None: ...
    def start_background(self, ctx: PluginContext) -> None: ...
    def on_event(self, event: Any, ctx: PluginContext) -> bool: ...


_REGISTRY: dict[str, Plugin] = {}
_HANDLERS: dict[str, Any] = {}


def register(plugin: Plugin, handler: Any | None = None) -> None:
    _REGISTRY[plugin.id] = plugin
    if handler is not None:
        _HANDLERS[plugin.id] = handler


def all_plugins() -> list[Plugin]:
    return list(_REGISTRY.values())


def get(plugin_id: str) -> Plugin | None:
    return _REGISTRY.get(plugin_id)


def get_handler(plugin_id: str) -> Any | None:
    return _HANDLERS.get(plugin_id)


def is_enabled(plugin_id: str, cfg: dict) -> bool:
    """Включён ли плагин в текущем конфиге.

    Если список `enabled_plugins` в конфиге отсутствует — используем
    `default_enabled` плагина (нужно для обратной совместимости со старыми
    конфигами, где этого поля ещё нет).
    """
    enabled = cfg.get("enabled_plugins")
    if enabled is None:
        plugin = _REGISTRY.get(plugin_id)
        return bool(plugin and plugin.default_enabled)
    return plugin_id in enabled


def set_enabled(plugin_id: str, cfg: dict, enable: bool) -> dict:
    """Возвращает обновлённый cfg с применённым изменением. Не сохраняет."""
    if cfg.get("enabled_plugins") is None:
        cfg["enabled_plugins"] = [
            p.id for p in _REGISTRY.values() if p.default_enabled
        ]
    enabled = list(cfg["enabled_plugins"])
    if enable and plugin_id not in enabled:
        enabled.append(plugin_id)
    elif not enable and plugin_id in enabled:
        enabled.remove(plugin_id)
    cfg["enabled_plugins"] = enabled
    return cfg


# ─── Диспатч плагинов ────────────────────────────────────────────

def setup_all(ctx: PluginContext) -> None:
    """Однократная инициализация всех включённых плагинов."""
    cfg = ctx.get_config()
    for pid, handler in _HANDLERS.items():
        if not is_enabled(pid, cfg):
            continue
        fn = getattr(handler, "setup", None)
        if fn is None:
            continue
        try:
            fn(ctx)
        except Exception:
            ctx.log.exception("plugin %s setup failed", pid)


def register_telegram_all(ctx: PluginContext) -> None:
    """Регистрирует Telegram-хендлеры у всех плагинов (вне зависимости от
    on/off — внутри хендлера сам плагин проверит, что он включён)."""
    for pid, handler in _HANDLERS.items():
        fn = getattr(handler, "register_telegram", None)
        if fn is None:
            continue
        try:
            fn(ctx)
        except Exception:
            ctx.log.exception("plugin %s register_telegram failed", pid)


def start_background_all(ctx: PluginContext) -> None:
    """Стартует фоновые задачи у включённых плагинов."""
    cfg = ctx.get_config()
    for pid, handler in _HANDLERS.items():
        if not is_enabled(pid, cfg):
            continue
        fn = getattr(handler, "start_background", None)
        if fn is None:
            continue
        try:
            fn(ctx)
        except Exception:
            ctx.log.exception("plugin %s start_background failed", pid)


def dispatch_event(event: Any, ctx: PluginContext) -> bool:
    """Прогоняет событие через все включённые плагины.

    Возвращает `True`, если хотя бы один плагин обработал событие (вернул
    `True` из `on_event`). Это сигнал ядру, что событие уже обработано — но
    ядро всё равно может выполнить свою универсальную логику (уведомления и
    т. п.), плагины не должны это блокировать.
    """
    cfg = ctx.get_config()
    handled = False
    for pid, handler in _HANDLERS.items():
        if not is_enabled(pid, cfg):
            continue
        fn = getattr(handler, "on_event", None)
        if fn is None:
            continue
        try:
            if fn(event, ctx):
                handled = True
        except Exception:
            ctx.log.exception("plugin %s on_event failed", pid)
    return handled


# ─── Регистрация плагинов ────────────────────────────────────────
# Импортируем плагины в самом низу, чтобы избежать циклов: каждый плагин
# импортирует константы/типы из этого модуля.
from . import autosteamoffline as _autosteamoffline  # noqa: E402
from . import autosteamrental as _autosteamrental  # noqa: E402
from . import authcode as _authcode  # noqa: E402
from . import autowithdraw as _autowithdraw  # noqa: E402

register(_autosteamoffline.PLUGIN, _autosteamoffline.HANDLER)
register(_autosteamrental.PLUGIN, _autosteamrental.HANDLER)
register(_authcode.PLUGIN, _authcode.HANDLER)
register(_autowithdraw.PLUGIN, _autowithdraw.HANDLER)

from . import chat_manager as _chat_manager  # noqa: E402
from . import reviews as _reviews  # noqa: E402
from . import deals as _deals  # noqa: E402
from . import autoconfirm as _autoconfirm  # noqa: E402
from . import items as _items  # noqa: E402

register(_chat_manager.PLUGIN, _chat_manager.HANDLER)
register(_reviews.PLUGIN, _reviews.HANDLER)
register(_deals.PLUGIN, _deals.HANDLER)
register(_autoconfirm.PLUGIN, _autoconfirm.HANDLER)
register(_items.PLUGIN, _items.HANDLER)

from . import custom_commands as _custom_commands  # noqa: E402
from . import proxy_manager as _proxy_manager  # noqa: E402
from . import multi_account as _multi_account  # noqa: E402

register(_custom_commands.PLUGIN, _custom_commands.HANDLER)
register(_proxy_manager.PLUGIN, _proxy_manager.HANDLER)
register(_multi_account.PLUGIN, _multi_account.HANDLER)

from . import smart_alerts as _smart_alerts  # noqa: E402

register(_smart_alerts.PLUGIN, _smart_alerts.HANDLER)
