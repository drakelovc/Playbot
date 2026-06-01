"""Настройка логирования: plain-text + JSON structured logs."""
from __future__ import annotations

import json
import logging
import os
from logging.handlers import TimedRotatingFileHandler

from core.config import LOG_FILE


class JsonFormatter(logging.Formatter):
    _STD_FIELDS = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in self._STD_FIELDS:
                continue
            try:
                json.dumps(v)
                base[k] = v
            except (TypeError, ValueError):
                base[k] = repr(v)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    log = logging.getLogger("playerok_bot")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    try:
        os.makedirs("logs", exist_ok=True)
        json_fh = TimedRotatingFileHandler(
            os.path.join("logs", "structured.log"),
            when="midnight", interval=1, backupCount=14, encoding="utf-8",
        )
        json_fh.suffix = "%Y-%m-%d"
        json_fh.setFormatter(JsonFormatter())
        log.addHandler(json_fh)
        for child in [
            "playerok_bot.autosteamrental",
            "playerok_bot.autosteamoffline",
            "playerok_bot.authcode",
            "autosteamrental.session",
        ]:
            logging.getLogger(child).addHandler(json_fh)
    except Exception:
        log.exception("Не удалось поднять структурное логирование")

    return log
