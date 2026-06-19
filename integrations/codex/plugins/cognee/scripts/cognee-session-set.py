#!/usr/bin/env python3
"""Switch the current launch's Cognee session (the picker's 'set' step).

Usage: ``cognee-session-set.py <session_id>``

A model-invoked command has no session id in its environment, so it discovers
its launch by walking the process tree to the host (claude/codex) pid, then maps
that to the launch's ``host_key`` (recorded at SessionStart). It then rewrites
only the ``session_id`` in that launch's map record — ``conn_uuid`` (the liveness
handle) is untouched, so registration/counting is unaffected.

On a real switch it also fires a detached sync of the *outgoing* session to the
graph (Feature 4), without unregistering. Prints a JSON result.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (  # noqa: E402
    find_host_pid,
    hook_log,
    read_host_key_for_pid,
    set_mapped_session,
)

try:
    from config import get_dataset, load_config
except Exception:  # pragma: no cover - config import is best-effort
    get_dataset = None
    load_config = None

_SYNC_SCRIPT = Path(__file__).with_name("sync-session-to-graph.py")


def _sync_on_switch_enabled() -> bool:
    return os.environ.get("COGNEE_SYNC_ON_SWITCH", "1").strip().lower() not in ("0", "false", "no")


def _spawn_switch_sync(old_session_id: str, host_key: str, dataset: str) -> None:
    """Detached, sync-only flush of the outgoing session (never unregisters)."""
    if not old_session_id or not _sync_on_switch_enabled():
        return
    try:
        env = os.environ.copy()
        env["COGNEE_SWITCH_SYNC"] = "1"  # sync-only: no once-claim, no unregister
        env["COGNEE_SYNC_SESSION_ID"] = old_session_id
        if host_key:
            env["COGNEE_SESSION_KEY"] = host_key
        if dataset:
            env["COGNEE_SYNC_DATASET"] = dataset
        env.pop("COGNEE_UNREGISTER_ON_FINISH", None)
        subprocess.Popen(
            [sys.executable, str(_SYNC_SCRIPT), "--detached-final"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        hook_log("switch_sync_spawn_failed", {"error": str(exc)[:200]})


def main() -> None:
    chosen = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not chosen:
        print(json.dumps({"ok": False, "error": "no session id provided"}))
        return

    host_key = read_host_key_for_pid(find_host_pid())
    if not host_key:
        print(json.dumps({"ok": False, "error": "could not determine current launch"}))
        return

    old, new = set_mapped_session(host_key, chosen)
    if not new:
        print(json.dumps({"ok": False, "error": "invalid session id"}))
        return

    dataset = ""
    try:
        if load_config and get_dataset:
            dataset = get_dataset(load_config())
    except Exception:
        pass

    switched = bool(old and old != new)
    if switched:
        _spawn_switch_sync(old, host_key, dataset)

    hook_log("session_switched", {"host_key": host_key, "old": old, "new": new})
    print(
        json.dumps(
            {
                "ok": True,
                "old_session": old,
                "new_session": new,
                "synced_previous": switched,
            }
        )
    )


if __name__ == "__main__":
    main()
