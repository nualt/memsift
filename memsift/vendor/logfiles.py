#!/usr/bin/env python3
# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Redacted append-only diagnostics, with one rotation and a literal 20 MB cap.

An unwritable destination cannot record its own failure. In that physical case
logging returns without raising, so the runner can still exit 0 without context.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import threading
import traceback
from typing import Optional

from .redact import redact

LOG_MAX_BYTES = 20_000_000
ROTATED_SUFFIX = ".1"
_logging = threading.local()


def rotated_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ROTATED_SUFFIX)


def _bounded_record(text: str, limit: int) -> bytes:
    if limit <= 0:
        raise ValueError("log byte cap must be positive")
    text = redact(text).replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    data = text.encode("utf-8")
    if len(data) > limit:
        marker = b"\n[truncated]\n"
        if limit < len(marker):
            return marker[-limit:]
        head = data[:limit - len(marker)].decode("utf-8", "ignore").encode("utf-8")
        data = head + marker
    return data


def _append_unlocked(path: Path, data: bytes, limit: int) -> None:
    if path.exists() and path.stat().st_size + len(data) > limit:
        os.replace(path, rotated_path(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise OSError("incomplete log append")
    finally:
        os.close(fd)


def append_line(path: Path, text: str, cap: Optional[int] = None) -> bool:
    """Redact, rotate before the byte cap and append a complete record once.

    Logging-lock contention uses a direct bounded append, rather than dropping
    the diagnostic. A thread-local guard prevents recursive logging failures.
    """
    path = Path(path)
    try:
        limit = LOG_MAX_BYTES if cap is None else cap
        data = _bounded_record(text, limit)
        path.parent.mkdir(parents=True, exist_ok=True)
        from .file_lock import file_lock

        # A failed logging lock must not prevent an otherwise writable diagnostic.
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(file_lock(path.with_name(path.name + ".lock"), timeout=0.05))
            except OSError as exc:
                error("log-lock", exc)
            _append_unlocked(path, data, limit)
        return True
    except Exception as exc:
        error("log-append", exc)
        return False


def error(event: str, exc: Exception, service: str = "", status: int = 0,
          duration: float = 0.0, response: str = "") -> None:
    if getattr(_logging, "active", False):
        return
    _logging.active = True
    try:
        message = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        details = redact(response)[:300]
        line = redact("ERROR %s %s status=%s duration=%.3f\n%s\n%s" %
                      (event, service, status, duration, details, message))
        append_line(Path.home() / ".memsift" / "debug.log", line)
    finally:
        _logging.active = False


def decision(event: str, details: dict) -> None:
    from .env_file import load_config

    if load_config()["MEMSIFT_DEBUG"] == "1":
        append_line(Path.home() / ".memsift" / "debug.log",
                    "DECISION %s %s" % (event, json.dumps(redact(details), ensure_ascii=False)))
