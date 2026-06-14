"""Structured logging.

Emits human-readable logs to the console and machine-readable JSON lines to
``logs/events.jsonl``. Includes a redaction filter so secrets can never leak
into a log file even if accidentally passed in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

# Patterns that must never appear in logs. We redact anything that looks like a
# private key or long hex secret regardless of where it came from.
_REDACT_PATTERNS = [
    re.compile(r"0x[a-fA-F0-9]{40,}"),         # addresses/keys
    re.compile(r"\b[a-fA-F0-9]{64}\b"),         # 32-byte hex (private keys)
    re.compile(r"sk-ant-[A-Za-z0-9\-_]+"),      # anthropic keys
]


def redact(text: str) -> str:
    for pat in _REDACT_PATTERNS:
        text = pat.sub("«REDACTED»", text)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _JsonLineHandler(logging.Handler):
    """Writes structured events as JSON lines for later analysis."""

    def __init__(self, path: str) -> None:
        super().__init__()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "event"):
            payload["event"] = getattr(record, "event")
        try:
            line = redact(json.dumps(payload, default=str))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # logging must never crash the app
            pass


_configured = False


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("polymanager")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(_RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"
    ))
    root.addHandler(console)
    root.addHandler(_JsonLineHandler(os.path.join(log_dir, "events.jsonl")))
    root.propagate = False
    _configured = True


def get_logger(name: str = "polymanager") -> logging.Logger:
    if not name.startswith("polymanager"):
        name = f"polymanager.{name}"
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **event: Any) -> None:
    """Log a message plus a structured ``event`` dict (goes to the JSONL sink)."""
    logger.log(level, msg, extra={"event": event})
