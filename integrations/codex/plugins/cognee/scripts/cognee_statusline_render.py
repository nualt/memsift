#!/usr/bin/env python3
"""Render the Cognee status line (Codex).

Invoked via ``cognee-statusline.sh``, which pipes a JSON context on stdin.
Deliberately standalone and pure-local: reads only env vars and
``~/.cognee-plugin/config.json`` — no network calls, no ``_plugin_common``
import.

Output: ``cognee: <dataset-name>``
"""

import json
import os
import sys
from pathlib import Path

_CONFIG_PATH = Path.home() / ".cognee-plugin" / "config.json"
_DEFAULT_DATASET = "cognee_sessions"


def _active_dataset() -> str:
    # 1. env var (inherited from the shell that launched Codex)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 2. config file
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            v = str(data.get("dataset") or "").strip()
            if v:
                return v
    except Exception:
        pass
    # 3. default
    return _DEFAULT_DATASET


def render_status_for_host(host_id: str) -> str:
    """Return the status string (host_id is unused; kept for call-site compat)."""
    return f"cognee: {_active_dataset()}"


def main() -> None:
    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(f"cognee: {_active_dataset()}")


if __name__ == "__main__":
    main()
