from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*basic\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|signature)\s*[:=]\s*)[^\s&,;]+"),
    re.compile(r"\b(?:sk|sess)-[A-Za-z0-9._-]{10,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "password",
    "secret",
    "session_secret",
    "signature",
    "token",
}


def _sensitive_key(value: object) -> bool:
    normalized = str(value).casefold()
    return normalized in _SENSITIVE_KEYS or any(
        marker in normalized
        for marker in (
            "api_key",
            "apikey",
            "authorization",
            "cookie",
            "password",
            "secret",
            "signature",
            "token",
        )
    )


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]" if pattern.groups else "[redacted]", redacted)
    return redacted


def sanitize_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return str(redact(value))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return str(redact(value))
    port = f":{parsed.port}" if parsed.port else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{parsed.hostname}{port}", parsed.path[:500], "", "")
    )


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "command": record.command,
            "run_id": record.run_id,
            "event": record.event,
            "message": redact(record.getMessage()),
            "context": redact(record.context),
        }
        if record.exc_info:
            document["traceback"] = redact(self.formatException(record.exc_info))
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = redact(record.context)
        suffix = ""
        if context:
            suffix = " " + " ".join(f"{key}={value}" for key, value in context.items())
        return f"{record.levelname:<7} [{record.event}] {redact(record.getMessage())}{suffix}"


class PipelineLog:
    def __init__(self, settings: Settings, db: Database, command: str, console: bool = True):
        self.settings = settings
        self.db = db
        self.command = command
        self.path = settings.data_dir / "logs" / "radio.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = db.execute(
            "INSERT INTO pipeline_run(command,status) VALUES(?, 'running')", (command,)
        )
        self._closed = False
        self._events: list[dict[str, Any]] = []
        self.logger = logging.getLogger(f"bsc_radio.pipeline.{self.run_id}")
        self.logger.handlers.clear()
        self.logger.propagate = False
        configured_level = os.getenv("RADIO_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, configured_level, logging.INFO)
        self.logger.setLevel(level)

        file_handler = RotatingFileHandler(
            self.path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        self.logger.addHandler(file_handler)
        if console:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(_ConsoleFormatter())
            self.logger.addHandler(console_handler)
        self.info("run.start", f"Démarrage de {command}", log_file=str(self.path))

    def _emit(
        self,
        level: int,
        event: str,
        message: str,
        *,
        exc_info: bool = False,
        **context: Any,
    ) -> None:
        safe_context = redact(context)
        self._events.append(
            {"event": event, "level": logging.getLevelName(level).lower(), **safe_context}
        )
        self.logger.log(
            level,
            message,
            exc_info=exc_info,
            extra={
                "command": self.command,
                "run_id": self.run_id,
                "event": event,
                "context": safe_context,
            },
        )

    def debug(self, event: str, message: str, **context: Any) -> None:
        self._emit(logging.DEBUG, event, message, **context)

    def info(self, event: str, message: str, **context: Any) -> None:
        self._emit(logging.INFO, event, message, **context)

    def warning(self, event: str, message: str, **context: Any) -> None:
        self._emit(logging.WARNING, event, message, **context)

    def exception(self, event: str, message: str, **context: Any) -> None:
        self._emit(logging.ERROR, event, message, exc_info=True, **context)

    @contextmanager
    def stage(self, name: str, **context: Any) -> Iterator[None]:
        started = time.monotonic()
        self.info(f"{name}.start", f"Étape {name} démarrée", **context)
        try:
            yield
        except Exception:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.exception(
                f"{name}.error", f"Étape {name} en échec", duration_ms=duration_ms, **context
            )
            raise
        else:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.info(f"{name}.done", f"Étape {name} terminée", duration_ms=duration_ms, **context)

    def finish(self, status: str, **detail: Any) -> None:
        if self._closed:
            return
        safe_detail = redact({**detail, "events": self._events[-100:]})
        self.info("run.finish", f"Fin de {self.command}", status=status, **detail)
        self.db.execute(
            """UPDATE pipeline_run SET status=?,finished_at=CURRENT_TIMESTAMP,detail_json=?
            WHERE id=?""",
            (status, json.dumps(safe_detail, ensure_ascii=False), self.run_id),
        )
        self._closed = True
        for handler in self.logger.handlers:
            handler.flush()
            handler.close()
        self.logger.handlers.clear()

    def fail(self, exc: Exception, **detail: Any) -> None:
        self.exception("run.error", "Échec du pipeline", error=type(exc).__name__, **detail)
        self.finish("failed", error=str(redact(str(exc))), **detail)


def tail_log(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as handle:
        return list(handle)[-lines:]
