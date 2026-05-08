#!/usr/bin/env python3
"""Store tool calls and assistant responses into the Cognee session cache.

Routes tool calls to the structured ``TraceEntry`` path (new trace-step
shape with origin_function / method_params / method_return_value /
status). Routes the final assistant message on Stop to a ``QAEntry``.

Runs async on the PostToolUse / Stop hooks — fire-and-forget, never
blocks Claude.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
    Falls back to COGNEE_SESSION_ID / COGNEE_PLUGIN_DATASET env vars.
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    append_http_bridge_entry,
    bump_save_counter,
    bump_turn_counter,
    hook_log,
    load_resolved,
    notify,
    persist_session_cache_to_graph_via_http,
    remember_entry_via_http,
    resolve_user,
    touch_activity,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
    persist_session_cache_to_graph,
    sync_graph_context_to_session,
)

# Hard cap per field to avoid ballooning the cache with massive tool outputs.
_MAX_PARAMS_BYTES = 4000
_MAX_RETURN_BYTES = 8000
_MAX_ASSISTANT_BYTES = 8000


async def _fire_improve_background(dataset: str, session_id: str, user, reason: str) -> None:
    """Fire-and-forget session bridge; failures are logged but never raised."""
    try:
        if is_cloud_mode(load_config()):
            wrote = persist_session_cache_to_graph_via_http(dataset, session_id)
            hook_log(
                "auto_bridge_fired",
                {"reason": reason, "session": session_id, "via": "http_remember", "wrote": wrote},
            )
            if wrote:
                notify(f"session bridge persisted ({reason})")
            return

        await ensure_dataset_ready(dataset, user)
        wrote = await persist_session_cache_to_graph(dataset, session_id, user)
        graph_result = await sync_graph_context_to_session(dataset, session_id, user)
        hook_log(
            "auto_bridge_fired",
            {
                "reason": reason,
                "session": session_id,
                "wrote": wrote,
                "graph_synced": graph_result.get("synced", 0),
            },
        )
        notify(f"session bridge persisted ({reason})")
    except Exception as exc:
        hook_log("auto_bridge_error", {"reason": reason, "error": str(exc)[:200]})


def _truncate_str(value, cap: int) -> str:
    """Coerce to string and cap at ``cap`` bytes (utf-8), appending ``...`` if truncated."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return text
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _infer_status(payload: dict) -> tuple[str, str]:
    """Return (status, error_message) from a PostToolUse payload."""
    # Claude Code sets tool_response.is_error=True on tool failures; also
    # check for an explicit 'error' key at the top level.
    response = payload.get("tool_response") or payload.get("tool_output") or ""
    if isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            err = response.get("error") or response.get("message") or "Tool reported an error."
            return "error", _truncate_str(err, 500)
    if isinstance(payload.get("error"), str) and payload["error"]:
        return "error", _truncate_str(payload["error"], 500)
    return "success", ""


def _load_session() -> tuple[str, str, str]:
    """Load session_id, dataset, user_id from resolved cache with fallbacks."""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


async def _store_tool_call(payload: dict) -> None:
    """Write a PostToolUse event as a TraceEntry."""
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input") or {}
    tool_output = payload.get("tool_output") or payload.get("tool_response") or ""

    # Suppress self-reference: any Bash call that mentions 'cognee' is
    # likely the plugin/CLI talking to itself and would recurse.
    if tool_name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command", ""))
        if "cognee" in cmd:
            hook_log("skip_self_cognee_bash", {"cmd_prefix": cmd[:80]})
            return

    status, error_message = _infer_status(payload)

    # Normalize method_params: small structured dict is ideal; fall back
    # to a truncated-string dict if we got something non-JSON-safe.
    if isinstance(tool_input, dict):
        params = {}
        for k, v in tool_input.items():
            params[k] = _truncate_str(v, _MAX_PARAMS_BYTES)
    else:
        params = {"value": _truncate_str(tool_input, _MAX_PARAMS_BYTES)}

    return_value = _truncate_str(tool_output, _MAX_RETURN_BYTES)

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"tool": tool_name})
        return

    config = load_config()
    await ensure_cognee_ready(config)

    entry = {
        "type": "trace",
        "origin_function": tool_name,
        "status": status,
        "method_params": params,
        "method_return_value": return_value,
        "error_message": error_message,
        # LLM-backed feedback per step is expensive on a busy session —
        # fall back to the deterministic one-liner. Users who want the
        # LLM summary can flip this in a future config.
        "generate_feedback_with_llm": False,
    }

    try:
        if is_cloud_mode(config):
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
        else:
            import cognee
            from cognee.memory import TraceEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                TraceEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("trace_store_error", {"tool": tool_name, "error": str(exc)[:200]})
        notify(f"trace store failed ({exc})")
        return

    if result:
        trace_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log(
            "trace_stored",
            {
                "tool": tool_name,
                "status": status,
                "trace_id": trace_id,
            },
        )
        notify(f"trace stored ({tool_name}, {status})")
        if is_cloud_mode(config):
            trace_text = (
                f"{tool_name} [{status}]\n"
                f"Params: {json.dumps(params, ensure_ascii=False)}\n"
                f"Return: {return_value}"
            )
            append_http_bridge_entry(
                dataset,
                session_id,
                trace=trace_text,
            )
        bump_save_counter(session_id, "trace")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")
    else:
        hook_log("trace_store_noresult", {"tool": tool_name})


async def _store_assistant_stop(payload: dict) -> None:
    """Write a Stop-hook payload (final assistant message) as a QAEntry."""
    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message") or "")
    if not msg or msg == "null":
        return

    msg = _truncate_str(msg, _MAX_ASSISTANT_BYTES)

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return

    config = load_config()
    await ensure_cognee_ready(config)

    # Answer-only QAEntry: we don't have the matching question at Stop
    # time. Leaving question="" keeps the entry searchable by the
    # assistant message text.
    entry = {"type": "qa", "question": "", "answer": msg, "context": ""}

    try:
        if is_cloud_mode(config):
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
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
        hook_log("stop_store_error", {"error": str(exc)[:200]})
        notify(f"stop store failed ({exc})")
        return

    if result:
        if is_cloud_mode(config):
            append_http_bridge_entry(dataset, session_id, answer=msg)
        qa_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log("stop_stored", {"chars": len(msg), "qa_id": qa_id})
        notify(f"assistant message stored ({len(msg)} chars)")
        bump_save_counter(session_id, "answer")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return

    is_stop = "--stop" in sys.argv
    try:
        if is_stop:
            asyncio.run(_store_assistant_stop(payload))
        else:
            asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
