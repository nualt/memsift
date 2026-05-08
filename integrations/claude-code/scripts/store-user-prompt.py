#!/usr/bin/env python3
"""Store the user's prompt into the Cognee session cache as a QAEntry.

Runs async on the UserPromptSubmit hook so it doesn't block the
parallel context-lookup hook.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    append_http_bridge_entry,
    bump_save_counter,
    hook_log,
    load_resolved,
    notify,
    remember_entry_via_http,
    resolve_user,
    touch_activity,
)
from config import ensure_cognee_ready, get_dataset, get_session_id, is_cloud_mode, load_config

MAX_TEXT = 4000
_WATCHER_PID = Path.home() / ".cognee-plugin" / "watcher.pid"
_WATCHER_STOP = Path.home() / ".cognee-plugin" / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")


def _load_session() -> tuple[str, str, str]:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _ensure_idle_watcher(session_id: str, dataset: str, config: dict) -> None:
    """Start the idle watcher on a new prompt if the prior one exited after bridging."""
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    if not session_id or _watcher_alive():
        return

    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception:
        pass

    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "config": {
            "service_url": config.get("service_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = Path.home() / ".cognee-plugin" / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception:
        log_fh = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("idle_watcher_restarted", {"session": session_id, "dataset": dataset})
    except Exception as exc:
        hook_log("idle_watcher_restart_failed", {"error": str(exc)[:200]})


async def _store(prompt: str):
    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "prompt"})
        return

    config = load_config()
    touch_activity()
    _ensure_idle_watcher(session_id, dataset, config)

    await ensure_cognee_ready(config)

    # Question-only QAEntry: the answer fills in on the Stop hook as
    # a separate entry. Keeping the prompt in the `question` field
    # lets recall's tokenizer search it naturally.
    entry = {"type": "qa", "question": prompt[:MAX_TEXT], "answer": "", "context": ""}

    try:
        if is_cloud_mode(config):
            result = remember_entry_via_http(dataset, session_id, entry)
        else:
            import cognee
            from cognee.memory import QAEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                QAEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("prompt_store_error", {"error": str(exc)[:200]})
        notify(f"prompt store failed ({exc})")
        return

    if result:
        if is_cloud_mode(config):
            append_http_bridge_entry(dataset, session_id, question=prompt[:MAX_TEXT])
        qa_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log("prompt_stored", {"chars": len(prompt), "qa_id": qa_id})
        notify(f"user prompt stored ({len(prompt)} chars)")
        bump_save_counter(session_id, "prompt")


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json", {"event": "prompt"})
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    try:
        asyncio.run(_store(prompt))
    except Exception as exc:
        hook_log("prompt_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
