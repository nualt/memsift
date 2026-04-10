#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Write resolved session ID to env cache for other hooks

The resolved session ID and dataset are written to a cache file
so that the other hook scripts (which run in separate processes)
can pick them up without re-computing.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from config import (
    load_config, get_session_id, get_dataset,
    ensure_cognee_ready, ensure_identity, save_config,
)


_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"


def _write_resolved(session_id: str, dataset: str, user_id: str, cwd: str) -> None:
    """Cache resolved session ID, dataset, and user ID for other hook scripts."""
    _RESOLVED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _RESOLVED_CACHE.write_text(json.dumps({
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "cwd": cwd,
    }, indent=2), encoding="utf-8")


async def _start():
    config = load_config()
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    session_id = get_session_id(config, cwd)
    dataset = get_dataset(config)

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Create integration identity (claude-code@cognee.local)
    user_id = ""
    try:
        user = await ensure_identity()
        user_id = str(user.id)
    except Exception as e:
        print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    # Write resolved values for other hooks
    _write_resolved(session_id, dataset, user_id, cwd)

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    mode = "cloud" if config.get("service_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )


def main():
    # Read stdin (SessionStart payload) — consumed but not used
    sys.stdin.read()

    try:
        asyncio.run(_start())
    except Exception as exc:
        print(f"cognee-plugin: session start failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
