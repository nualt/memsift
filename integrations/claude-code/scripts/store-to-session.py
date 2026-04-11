#!/usr/bin/env python3
"""Store text into a Cognee session cache (lightweight, no cognify).

Usage:
    echo '{"tool_name":"Read","tool_input":{},"tool_output":"..."}' | python store-to-session.py
    echo '{"assistant_message":"..."}' | python store-to-session.py --stop

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
    Falls back to COGNEE_SESSION_ID / COGNEE_PLUGIN_DATASET env vars.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from config import load_config, get_session_id, get_dataset


_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
MAX_TEXT = 4000


def _load_resolved() -> tuple:
    """Load session ID, dataset, and user ID from resolved cache."""
    if _RESOLVED_CACHE.exists():
        try:
            data = json.loads(_RESOLVED_CACHE.read_text(encoding="utf-8"))
            return data.get("session_id", ""), data.get("dataset", ""), data.get("user_id", "")
        except Exception:
            pass
    config = load_config()
    return get_session_id(config), get_dataset(config), ""


def _build_tool_text(payload: dict) -> str:
    tool_name = payload.get("tool_name", "unknown")
    tool_input = json.dumps(payload.get("tool_input", {}))[:MAX_TEXT]
    tool_response = str(payload.get("tool_output") or payload.get("tool_response", ""))[:MAX_TEXT]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[category:agent] [{ts}] Tool: {tool_name}\nInput: {tool_input}\nOutput: {tool_response}"


def _build_stop_text(payload: dict) -> str:
    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message", ""))[
        :MAX_TEXT
    ]
    if not msg or msg == "null":
        return ""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[category:agent] [{ts}] Assistant response:\n{msg}"


async def _resolve_user(user_id: str):
    """Resolve cached user ID to a User object, or fall back to default."""
    if user_id:
        try:
            from uuid import UUID
            from cognee.modules.users.methods import get_user
            user = await get_user(UUID(user_id))
            if user:
                return user
        except Exception:
            pass
    from cognee.modules.users.methods import get_default_user
    return await get_default_user()


async def _store(text: str, session_id: str, dataset: str, user_id: str):
    """Call cognee.remember with session_id for lightweight session storage."""
    import cognee

    user = await _resolve_user(user_id)
    result = await cognee.remember(
        data=text,
        dataset_name=dataset,
        session_id=session_id,
        user=user,
    )

    if not result:
        status = getattr(result, "status", "unknown")
        print(
            f"cognee-session: store failed (status={status})",
            file=sys.stderr,
        )


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    is_stop = "--stop" in sys.argv

    # Skip recursive cognee-cli / cognee calls
    if not is_stop:
        tool_name = payload.get("tool_name", "")
        tool_input_str = json.dumps(payload.get("tool_input", {}))
        if tool_name == "Bash" and "cognee" in tool_input_str:
            return
        text = _build_tool_text(payload)
    else:
        text = _build_stop_text(payload)

    if not text:
        return

    session_id, dataset, user_id = _load_resolved()
    asyncio.run(_store(text, session_id, dataset, user_id))


if __name__ == "__main__":
    main()
