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
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for config/_plugin_common imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, improve_via_http, sync_lock
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    load_config,
    persist_session_cache_to_graph,
)

_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
_WATCHER_PID = Path.home() / ".cognee-plugin" / "watcher.pid"
_WATCHER_STOP = Path.home() / ".cognee-plugin" / "watcher.stop"
_DETACHED_ARG = "--detached-final"


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


def _spawn_detached_sync() -> bool:
    """Run the expensive sync outside Claude's short SessionEnd hook window."""
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _DETACHED_ARG],
            cwd=os.getcwd(),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        hook_log("sync_detach_failed", {"error": str(exc)[:300]})
        return False


def _is_session_end_payload(payload_raw: str) -> bool:
    """Return True only for an actual Claude Code SessionEnd hook payload."""
    if not payload_raw.strip():
        return False
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return False

    def _contains_session_end(value) -> bool:
        if isinstance(value, dict):
            return any(_contains_session_end(item) for item in value.values())
        if isinstance(value, list):
            return any(_contains_session_end(item) for item in value)
        if isinstance(value, str):
            return value == "SessionEnd" or value.endswith(".SessionEnd")
        return False

    event = (
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or payload.get("hook")
    )
    return event == "SessionEnd" or _contains_session_end(payload)


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


async def _sync(stop_watcher: bool):
    import cognee

    session_id, dataset, user_id = _load_resolved()
    hook_log(
        "sync_start",
        {"session": session_id, "dataset": dataset, "stop_watcher": stop_watcher},
    )

    with sync_lock("sync-session-to-graph") as acquired:
        if not acquired:
            hook_log("sync_skipped_lock_busy", {"session": session_id, "dataset": dataset})
            print("cognee-sync: skipped, another sync is running", file=sys.stderr)
            return

        if stop_watcher:
            _stop_idle_watcher()
            hook_log("sync_stopped_watcher", {"session": session_id, "dataset": dataset})

        # Prefer the running backend to avoid the Kuzu single-writer lock.
        if improve_via_http(dataset, session_id, run_in_background=False):
            hook_log("sync_http_done", {"session": session_id, "dataset": dataset})
            print(
                f"cognee-sync: via HTTP dataset={dataset} session={session_id}",
                file=sys.stderr,
            )
            return

        # Fallback: no backend running → run improve() locally via the SDK.
        config = load_config()
        await ensure_cognee_ready(config)
        user = await _resolve_user(user_id)
        await ensure_dataset_ready(dataset, user)
        await persist_session_cache_to_graph(dataset, session_id, user)

        result = await cognee.improve(
            dataset=dataset,
            session_ids=[session_id],
            run_in_background=False,
            user=user,
        )

        hook_log("sync_sdk_done", {"session": session_id, "dataset": dataset})
        # Log summary to stderr (visible in hook output, not in Claude's context)
        if result and isinstance(result, dict):
            for ds_id, run_info in result.items():
                status = getattr(run_info, "status", "unknown")
                print(f"cognee-sync: dataset={ds_id} status={status}", file=sys.stderr)
        else:
            print(f"cognee-sync: dataset={dataset} session={session_id} completed", file=sys.stderr)


def main():
    detached_final = _DETACHED_ARG in sys.argv
    payload_raw = "" if detached_final else sys.stdin.read()
    is_session_end = _is_session_end_payload(payload_raw)
    hook_log(
        "sync_payload",
        {
            "is_session_end": is_session_end,
            "detached_final": detached_final,
            "payload_preview": payload_raw[:200],
        },
    )

    # Only a true SessionEnd should stop the watcher. Manual syncs and
    # slash-command invocations happen mid-session, and killing the watcher
    # there prevents later idle persistence.
    if is_session_end:
        spawned = _spawn_detached_sync()
        _stop_idle_watcher()
        hook_log("sync_deferred_to_shutdown_worker", {"spawned": spawned})
        return

    try:
        asyncio.run(_sync(stop_watcher=False))
    except Exception as exc:
        # Non-fatal: session sync failure should not crash Claude Code
        hook_log("sync_failed", {"error": str(exc)[:300]})
        print(f"cognee-sync: failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    main()
