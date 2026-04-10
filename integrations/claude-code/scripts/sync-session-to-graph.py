#!/usr/bin/env python3
"""Bridge session cache entries into the permanent knowledge graph on session end.

Calls cognee.improve(session_ids=[...]) to run:
  1. Apply feedback weights from session scores
  2. Persist session Q&A into the permanent graph
  3. Default enrichment (triplet embeddings)
  4. Sync graph knowledge back into session cache

Configuration:
    Uses resolved session ID and dataset from SessionStart hook
    (via ~/.cognee-plugin/resolved.json). Falls back to env vars.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from config import load_config, get_session_id, get_dataset


_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"


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


async def _sync():
    import cognee

    session_id, dataset, user_id = _load_resolved()
    user = await _resolve_user(user_id)

    result = await cognee.improve(
        dataset=dataset,
        session_ids=[session_id],
        run_in_background=False,
        user=user,
    )

    # Log summary to stderr (visible in hook output, not in Claude's context)
    if result and isinstance(result, dict):
        for ds_id, run_info in result.items():
            status = getattr(run_info, "status", "unknown")
            print(f"cognee-sync: dataset={ds_id} status={status}", file=sys.stderr)
    else:
        print(f"cognee-sync: dataset={dataset} session={session_id} completed", file=sys.stderr)


def main():
    # Read stdin (SessionEnd payload) but we only use config for IDs
    sys.stdin.read()

    try:
        asyncio.run(_sync())
    except Exception as exc:
        # Non-fatal: session sync failure should not crash Claude Code
        print(f"cognee-sync: failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
