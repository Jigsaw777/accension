"""Bounded local metadata logs. Prompt bodies and arbitrary trace payloads are dropped."""

from __future__ import annotations

import contextvars
import json
import logging
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from .safety import redact as redact_known

correlation = contextvars.ContextVar("accension_log_context", default=None)
FIELDS = {
    "request_id",
    "session_id",
    "run_id",
    "plan_id",
    "task_id",
    "provider",
    "model",
    "client",
    "duration_ms",
    "error_type",
    "error_id",
    "method",
    "route",
    "status",
    "exit_code",
    "input_tokens",
    "output_tokens",
    "attempt",
    "passed",
    "command",
    "frames",
    "count",
    "stage",
}
PRIVATE_KEYS = re.compile(
    r"(?i)(auth|cookie|password|secret|token|credential|vault|api.?key|prompt|source|content|body|messages)"
)


def redact(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if PRIVATE_KEYS.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = redact_known(value)
        value = re.sub(
            r"(?i)(authorization|cookie|set-cookie|password|api[-_]?key|gateway[-_]?token|vault)\s*[:=]\s*[^\r\n]+",
            "[REDACTED]",
            value,
        )
        value = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[REDACTED]", value)
        value = re.sub(r"https?://[^\s?]+\?[^\s]+", "[URL WITH QUERY REDACTED]", value)
        # Absolute paths can expose usernames, repository names and private locations.
        value = re.sub(r"(?:[A-Za-z]:[\\/]|/Users/|/home/)[^\s\"']+", "[LOCAL PATH]", value)
        return value
    return value if isinstance(value, (int, float, bool)) or value is None else "[UNSUPPORTED]"


@contextmanager
def scope(**fields):
    token = correlation.set({**(correlation.get() or {}), **fields})
    try:
        yield
    finally:
        correlation.reset(token)


_write_lock = threading.RLock()


class SafeHandler(RotatingFileHandler):
    failed = False

    def emit(self, record):
        # CLI and gateway objects may share a home. Close after each append so
        # another handler can rotate the file on Windows.
        with _write_lock:
            try:
                super().emit(record)
            finally:
                self.close()

    def handleError(self, record):
        # Never print the record or exception (logging's default exposes both).
        if not self.failed:
            self.failed = True
            print("Accension could not write local logs. Check disk space and directory permissions.", file=sys.stderr)


class EventLog:
    def __init__(self, settings):
        self.path = settings.home / "logs" / "accension.jsonl"
        self.logger = logging.Logger("accension." + uuid4().hex, level=settings.log_level)
        self.logger.propagate = False
        self.handler = None
        self.error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            self.handler = SafeHandler(
                self.path,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backups,
                encoding="utf-8",
                delay=True,
            )
            self.path.touch(exist_ok=True)
            self.path.chmod(0o600)
            self.handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(self.handler)
        except OSError as exc:
            self.error = type(exc).__name__
            self.logger.addHandler(logging.NullHandler())
            print("Accension logs are unavailable. Check the home directory permissions.", file=sys.stderr)

    def emit(self, component, event, level="INFO", **fields):
        try:
            safe = {k: v for k, v in {**(correlation.get() or {}), **fields}.items() if k in FIELDS}
            # Token counts are numbers, never credentials.
            safe = {
                k: v if k in {"input_tokens", "output_tokens"} and isinstance(v, int) else redact(v)
                for k, v in safe.items()
            }
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "component": redact(component),
                "event": redact(event),
                **safe,
            }
            self.logger.log(getattr(logging, level, logging.INFO), json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            if not self.error:
                self.error = "LogWriteError"
                print("Accension could not record a log event. Check disk space.", file=sys.stderr)

    def failure(self, component, exc, **fields):
        error_id = uuid4().hex[:16]
        self.emit(component, "error", "ERROR", error_id=error_id, error_type=type(exc).__name__, **fields)
        if self.logger.isEnabledFor(logging.DEBUG):
            # Frame metadata only: no exception message, source lines or local variables.
            frames = [
                {"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                for f in traceback.extract_tb(exc.__traceback__)
            ]
            self.emit(component, "exception_frames", "DEBUG", error_id=error_id, frames=frames, **fields)
        return error_id

    def status(self):
        return {
            "directory": str(self.path.parent),
            "writable": self.handler is not None and not self.handler.failed,
            "level": logging.getLevelName(self.logger.level),
            "error": self.error,
            "max_bytes": self.handler.maxBytes if self.handler else None,
            "backups": self.handler.backupCount if self.handler else None,
        }

    def close(self):
        for handler in self.logger.handlers:
            handler.close()


def read_logs(settings, *, limit=100, level=None, component=None, run=None, since=None):
    path = settings.home / "logs" / "accension.jsonl"
    items = deque(maxlen=max(1, min(limit, 5000)))
    if since:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        since = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    paths = [path.with_name(path.name + f".{n}") for n in range(settings.log_backups, 0, -1)] + [path]
    for current in paths:
        try:
            with current.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if level and item.get("level") != level.upper() or component and item.get("component") != component:
                        continue
                    if run and run not in [item.get(k) for k in ("run_id", "request_id", "plan_id", "error_id")]:
                        continue
                    if since and item.get("timestamp", "") < since:
                        continue
                    items.append(item)
        except FileNotFoundError:
            continue
    return list(items)


def export_bundle(settings, output):
    from . import __version__

    # Explicit allowlist: never archive config, vaults, state DBs, source or skill files.
    items = read_logs(settings, limit=5000)
    safe_items = [
        {k: redact(v) for k, v in item.items() if k in FIELDS | {"timestamp", "level", "component", "event"}}
        for item in items
    ]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("logs.json", json.dumps(safe_items, indent=2))
        archive.writestr(
            "diagnostics.json",
            json.dumps(
                {
                    "version": __version__,
                    "python": platform.python_version(),
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "created_at": time.time(),
                    "mock": settings.mock,
                    "providers": len(settings.providers),
                    "models": len(settings.models),
                }
            ),
        )
    return {"exported": str(Path(output).resolve()), "note": "Review the redacted bundle before sharing it"}
