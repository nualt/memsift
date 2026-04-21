#!/usr/bin/env python3
"""Bridge session cache entries into the permanent knowledge graph on session end.

Calls cognee.improve(session_ids=[...]) to run:
  1. Apply feedback weights from session scores
  2. Persist session Q&A into the permanent graph
  3. Default enrichment (triplet embeddings)
  4. Sync graph knowledge back into session cache

Execution path:
    1. If a local backend is running (COGNEE_LOCAL_API_URL or
       http://localhost:8000), POST to /api/v1/improve so the server
       — which holds the Kuzu single-writer lock — runs the pipeline.
    2. Otherwise, fall back to direct cognee.improve() SDK call.

Configuration:
    Uses resolved session ID and dataset from SessionStart hook
    (via ~/.cognee-plugin/resolved.json). Falls back to env vars.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

# Add scripts dir to path for config/_plugin_common imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import improve_via_http
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
_WATCHER_PID = Path.home() / ".cognee-plugin" / "watcher.pid"
_WATCHER_STOP = Path.home() / ".cognee-plugin" / "watcher.stop"


def _stop_idle_watcher() -> None:
    """Signal the idle watcher to exit and drop its pidfile.

    Uses both a sentinel file (safe, polled by the watcher) and a
    SIGTERM (fast). Either path is sufficient; both together handle
    the SIGTERM-blocked-during-improve edge case.
    """
    try:
        _WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
        _WATCHER_STOP.write_text("stop", encoding="utf-8")
    except Exception:
        pass
    if _WATCHER_PID.exists():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


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

    # Prefer the running backend to avoid the Kuzu single-writer lock.
    if improve_via_http(dataset, session_id, run_in_background=True):
        print(
            f"cognee-sync: via HTTP dataset={dataset} session={session_id}",
            file=sys.stderr,
        )
        return

    # Fallback: no backend running → run improve() locally via the SDK.
    config = load_config()
    await ensure_cognee_ready(config)
    user = await _resolve_user(user_id)

    result = await cognee.improve(
        dataset=dataset,
        session_ids=[session_id],
        run_in_background=True,
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

    # Stop the idle watcher first — we're about to run a blocking
    # improve() ourselves and don't want a racing one from the watcher.
    _stop_idle_watcher()

    try:
        asyncio.run(_sync())
    except Exception as exc:
        # Non-fatal: session sync failure should not crash Claude Code
        print(f"cognee-sync: failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
