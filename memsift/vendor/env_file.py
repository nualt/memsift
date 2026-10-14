# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Read the nine memsift settings, with environment precedence and no side effects."""

from __future__ import annotations

import os
from pathlib import Path

from .logfiles import error

_DEFAULTS = {
    "COGNEE_BASE_URL": "",
    "COGNEE_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "MEMSIFT_JEV_MODEL": "typesafe/jev-1.13",
    "MEMSIFT_COMPACTOR_URL": "https://openrouter.ai/api/v1",
    "MEMSIFT_COMPACTOR_API_KEY": "",
    "MEMSIFT_COMPACTOR_MODEL": "anthropic/claude-haiku-5.5",
    "MEMSIFT_DATASET": "memsift",
    "MEMSIFT_DEBUG": "0",
}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(path: Path) -> dict:
    """Parse a dotenv-style file into a dict. Malformed lines are skipped.

    Supported: ``KEY=VALUE``, blank lines, ``#`` comments, an optional leading
    ``export ``, and single/double quotes around the value. No interpolation,
    no multi-line values.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        # utf-8-sig strips a UTF-8 BOM when present (Windows PowerShell 5.1
        # writes one) and is a no-op otherwise. A PS5 `>` redirect produces
        # UTF-16 instead; retry with that before giving up.
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            error("config-decode", exc)
            text = path.read_text(encoding="utf-16")
    except (OSError, UnicodeError) as exc:
        error("config-read", exc)
        return result

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        result[key] = _unquote(value)
    return result


def load_config() -> dict:
    values = parse_env_file(Path.home() / ".cognee" / ".env")
    config = {key: os.environ.get(key, values.get(key, default)) for key, default in _DEFAULTS.items()}
    if not config["MEMSIFT_COMPACTOR_API_KEY"]:
        config["MEMSIFT_COMPACTOR_API_KEY"] = config["OPENROUTER_API_KEY"]
    return config
