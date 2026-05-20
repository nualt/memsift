#!/usr/bin/env python3
"""Forward Codex hook events into Cognee session memory.

The hook is intentionally stdlib-only and fail-open: if Cognee is not reachable
or credentials are not configured, Codex continues normally.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "codex_sessions"
DEFAULT_SERVICE_URL = "http://localhost:8000"
DEFAULT_BACKEND_MODE = "auto"
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_RECALL_TIMEOUT_SECONDS = 3.0
DEFAULT_IMPROVE_TIMEOUT_SECONDS = 2.0
DEFAULT_RECALL_TOP_K = 5
DEFAULT_RECALL_SCOPE = ("session", "trace", "graph_context", "graph")
DEFAULT_AUTO_IMPROVE_EVERY = 30
MAX_STRING_BYTES = 4000
MAX_RETURN_BYTES = 8000
MAX_CONTEXT_BYTES = 8000
MAX_RECALL_RESPONSE_BYTES = 262144
MAX_SESSION_PREVIEW_BYTES = 520
MAX_TRACE_PREVIEW_BYTES = 360
MAX_GRAPH_PREVIEW_BYTES = 900
MAX_CONTAINER_ITEMS = 40
MAX_DEPTH = 5
STATE_DIR = Path.home() / ".cognee"
SAVE_COUNTER_PATH = STATE_DIR / "codex-save-counter.json"
LAST_RECALL_PATH = STATE_DIR / "codex-last-recall.json"
RECALL_AUDIT_PATH = STATE_DIR / "codex-recall-audit.log"
AUTO_IMPROVE_PATH = STATE_DIR / "codex-auto-improve.json"
PENDING_PROMPTS_PATH = STATE_DIR / "codex-pending-prompts.json"
SAVE_KINDS = ("prompt", "trace", "answer")
RECALL_SOURCES = ("session", "trace", "graph_context")
RECALL_SECTION_LIMITS = {"session": 3, "trace": 2, "graph_context": 2}
AUTO_IMPROVE_EVENTS = {"PostToolUse", "Stop"}

SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bck_[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(event: str, detail: dict[str, Any] | None = None) -> None:
    """Best-effort local hook log without secret values."""
    try:
        log_path = Path.home() / ".cognee" / "codex-hook.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line: dict[str, Any] = {"ts": _now(), "pid": os.getpid(), "event": event}
        if detail:
            line["detail"] = _sanitize(detail)
        serialized = json.dumps(line, sort_keys=True, default=str)
        if len(serialized) > 1200:
            serialized = serialized[:1197] + "..."
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
    except Exception:
        pass


def _debug_enabled() -> bool:
    return os.environ.get("COGNEE_CODEX_HOOK_DEBUG", "").lower() in {"1", "true", "yes"}


def _debug(message: str) -> None:
    if _debug_enabled():
        print(f"cognee-codex-hook: {message}", file=sys.stderr)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _backend_mode() -> str:
    raw = os.environ.get("COGNEE_CODEX_BACKEND", DEFAULT_BACKEND_MODE).strip().lower()
    if raw in {"http", "api", "remote"}:
        return "http"
    if raw in {"native", "sdk", "local"}:
        return "native"
    return DEFAULT_BACKEND_MODE


def _explicit_http_connection_configured() -> bool:
    return bool(
        os.environ.get("COGNEE_SERVICE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or os.environ.get("COGNEE_API_KEY")
    )


def _truncate(text: Any, max_bytes: int = MAX_STRING_BYTES) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max_bytes - 15].decode("utf-8", errors="ignore") + "...[truncated]"


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _sanitize(value: Any, depth: int = 0, *, max_string_bytes: int = MAX_STRING_BYTES) -> Any:
    if depth > MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= MAX_CONTAINER_ITEMS:
                out["[truncated_keys]"] = len(value) - MAX_CONTAINER_ITEMS
                break
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = _sanitize(item, depth + 1, max_string_bytes=max_string_bytes)
        return out
    if isinstance(value, (list, tuple)):
        items = [
            _sanitize(item, depth + 1, max_string_bytes=max_string_bytes)
            for item in list(value)[:MAX_CONTAINER_ITEMS]
        ]
        if len(value) > MAX_CONTAINER_ITEMS:
            items.append(f"[truncated {len(value) - MAX_CONTAINER_ITEMS} items]")
        return items
    if isinstance(value, str):
        return _truncate(_redact_string(value), max_string_bytes)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate(_redact_string(str(value)), max_string_bytes)


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log("invalid_json", {"error": str(exc)})
        return {}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _zero_save_counts() -> dict[str, int]:
    return {kind: 0 for kind in SAVE_KINDS}


def _zero_recall_counts() -> dict[str, int]:
    return {source: 0 for source in RECALL_SOURCES}


def _save_kind(event: str) -> str:
    if event == "UserPromptSubmit":
        return "prompt"
    if event == "Stop":
        return "answer"
    return "trace"


def _bump_save_counter(session_id: str, kind: str) -> None:
    if not session_id or kind not in SAVE_KINDS:
        return
    try:
        data = _load_json_file(SAVE_COUNTER_PATH)
        session_counts = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
        counts = _zero_save_counts()
        counts.update({key: int(session_counts.get(key, 0)) for key in SAVE_KINDS})
        counts[kind] += 1
        data[session_id] = counts
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_COUNTER_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _read_and_reset_save_counter(session_id: str) -> dict[str, int]:
    if not session_id:
        return _zero_save_counts()
    try:
        data = _load_json_file(SAVE_COUNTER_PATH)
        raw_counts = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
        counts = _zero_save_counts()
        counts.update({key: int(raw_counts.get(key, 0)) for key in SAVE_KINDS})
        data[session_id] = _zero_save_counts()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SAVE_COUNTER_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        return counts
    except Exception:
        return _zero_save_counts()


def _pending_prompt_keys(payload: dict[str, Any]) -> tuple[str, str]:
    session_id = _session_id(payload)
    turn_id = str(payload.get("turn_id") or "").strip()
    session_key = f"{session_id}:"
    turn_key = f"{session_id}:{turn_id}" if turn_id else session_key
    return turn_key, session_key


def _remember_pending_prompt(payload: dict[str, Any]) -> None:
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        return
    try:
        turn_key, session_key = _pending_prompt_keys(payload)
        data = _load_json_file(PENDING_PROMPTS_PATH)
        entry = {
            "prompt": _truncate(prompt, MAX_RETURN_BYTES),
            "context": _memory_context(payload),
            "ts": _now(),
        }
        data[turn_key] = entry
        data[session_key] = entry
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_PROMPTS_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _pop_pending_prompt(payload: dict[str, Any]) -> dict[str, str]:
    try:
        turn_key, session_key = _pending_prompt_keys(payload)
        data = _load_json_file(PENDING_PROMPTS_PATH)
        entry = data.pop(turn_key, None) or data.get(session_key) or {}
        data.pop(session_key, None)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_PROMPTS_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        if isinstance(entry, dict):
            return {
                "prompt": str(entry.get("prompt") or ""),
                "context": str(entry.get("context") or ""),
            }
    except Exception:
        pass
    return {"prompt": "", "context": ""}


def _auto_improve_enabled() -> bool:
    if _env_bool("COGNEE_CODEX_AUTO_IMPROVE_DISABLED"):
        return False
    return _env_bool("COGNEE_CODEX_AUTO_IMPROVE", True)


def _auto_improve_every() -> int:
    raw = os.environ.get("COGNEE_CODEX_AUTO_IMPROVE_EVERY") or os.environ.get(
        "COGNEE_AUTO_IMPROVE_EVERY", ""
    )
    try:
        value = int(raw) if raw else DEFAULT_AUTO_IMPROVE_EVERY
    except ValueError:
        value = DEFAULT_AUTO_IMPROVE_EVERY
    return max(0, min(value, 10000))


def _bump_auto_improve_counter(session_id: str, event: str) -> tuple[int, str]:
    """Return (event_count, reason) when this event should trigger improve."""
    if not session_id or event not in AUTO_IMPROVE_EVENTS or not _auto_improve_enabled():
        return 0, ""

    try:
        data = _load_json_file(AUTO_IMPROVE_PATH)
        state = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
        count = int(state.get("count", 0)) + 1
        stop_improved = bool(state.get("stop_improved", False))
        reason = ""

        if event == "Stop" and not stop_improved:
            reason = "first_stop"
            stop_improved = True
        else:
            threshold = _auto_improve_every()
            if threshold > 0 and count % threshold == 0:
                reason = f"event_{count}"

        data[session_id] = {
            "count": count,
            "stop_improved": stop_improved,
            "last_reason": reason or state.get("last_reason", ""),
            "updated_at": _now(),
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        AUTO_IMPROVE_PATH.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        return count, reason
    except Exception:
        return 0, ""


def _load_connection() -> tuple[str, str]:
    service_url = (
        os.environ.get("COGNEE_SERVICE_URL") or os.environ.get("COGNEE_LOCAL_API_URL") or ""
    ).strip()
    api_key = os.environ.get("COGNEE_API_KEY", "").strip()

    cloud = _load_json_file(Path.home() / ".cognee" / "cloud_credentials.json")
    plugin = _load_json_file(Path.home() / ".cognee-plugin" / "config.json")

    service_url = service_url or str(cloud.get("service_url") or plugin.get("service_url") or "")
    api_key = api_key or str(cloud.get("api_key") or plugin.get("api_key") or "")

    return (service_url.rstrip("/") or DEFAULT_SERVICE_URL, api_key)


def _dataset_name() -> str:
    return (
        os.environ.get("COGNEE_CODEX_DATASET")
        or os.environ.get("COGNEE_PLUGIN_DATASET")
        or DEFAULT_DATASET
    )


def _session_id(payload: dict[str, Any]) -> str:
    direct = (
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("thread_id")
        or os.environ.get("CODEX_SESSION_ID")
        or os.environ.get("COGNEE_SESSION_ID")
    )
    if direct:
        return _truncate(str(direct), 160)

    cwd = str(payload.get("cwd") or os.getcwd())
    digest = hashlib.sha256(cwd.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"codex_{Path(cwd).name or 'session'}_{digest}"


def _status(payload: dict[str, Any]) -> tuple[str, str]:
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            return "error", _truncate(
                response.get("error") or response.get("message") or response,
                700,
            )
        for key in ("exit_code", "status_code"):
            try:
                if int(response.get(key, 0)) != 0:
                    return "error", _truncate(response, 700)
            except Exception:
                pass

    if payload.get("error"):
        return "error", _truncate(payload.get("error"), 700)

    return "success", ""


def _event_name(cli_event: str | None, payload: dict[str, Any]) -> str:
    return (
        cli_event
        or payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or payload.get("type")
        or "CodexHook"
    )


def _origin(event: str, payload: dict[str, Any]) -> str:
    if event == "PostToolUse":
        tool = payload.get("tool_name") or payload.get("tool") or "tool"
        return f"codex.tool.{tool}"
    return f"codex.{event}"


def _return_value(event: str, payload: dict[str, Any]) -> Any:
    if event == "SessionStart":
        return {
            "source": payload.get("source"),
            "message": "Codex session started.",
        }
    if event == "UserPromptSubmit":
        return {
            "prompt": _sanitize(payload.get("prompt", ""), max_string_bytes=MAX_RETURN_BYTES),
        }
    if event == "PostToolUse":
        return _sanitize(
            payload.get("tool_response", payload.get("tool_output", "")),
            max_string_bytes=MAX_RETURN_BYTES,
        )
    if event == "Stop":
        return {
            "last_assistant_message": _sanitize(
                payload.get("last_assistant_message") or payload.get("assistant_message") or "",
                max_string_bytes=MAX_RETURN_BYTES,
            )
        }
    return _sanitize(payload, max_string_bytes=MAX_RETURN_BYTES)


def _method_params(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = {
        "event": event,
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "transcript_path": payload.get("transcript_path"),
    }
    if event == "PostToolUse":
        base["tool_name"] = payload.get("tool_name")
        base["tool_input"] = payload.get("tool_input")
    elif event == "UserPromptSubmit":
        base["prompt"] = payload.get("prompt")
    elif event == "SessionStart":
        base["source"] = payload.get("source")
    elif event == "Stop":
        base["stop_hook_active"] = payload.get("stop_hook_active")
    else:
        base["payload"] = payload
    return _sanitize(base)


def _memory_query(event: str, payload: dict[str, Any]) -> str:
    if event == "UserPromptSubmit":
        return _truncate(payload.get("prompt", ""), 1200)
    if event == "PostToolUse":
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            return _truncate(tool_input.get("command") or tool_input, 1200)
        return _truncate(tool_input or payload.get("tool_name") or "", 1200)
    return _truncate(event, 1200)


def _memory_context(payload: dict[str, Any]) -> str:
    context = {
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "turn_id": payload.get("turn_id"),
        "transcript_path": payload.get("transcript_path"),
    }
    return _truncate(_sanitize(context), 1600)


def _entry(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event == "UserPromptSubmit":
        return {
            "type": "trace",
            "origin_function": _origin(event, payload),
            "status": "success",
            "method_params": _method_params(event, payload),
            "method_return_value": {"prompt_saved_for_stop": bool(payload.get("prompt"))},
            "memory_query": _memory_query(event, payload),
            "memory_context": _memory_context(payload),
            "error_message": "",
            "generate_feedback_with_llm": False,
        }

    if event == "Stop":
        pending_prompt = _pop_pending_prompt(payload)
        return {
            "type": "qa",
            "question": pending_prompt["prompt"],
            "answer": _truncate(
                payload.get("last_assistant_message") or payload.get("assistant_message") or "",
                MAX_RETURN_BYTES,
            ),
            "context": pending_prompt["context"] or _memory_context(payload),
        }

    status, error_message = _status(payload)
    return {
        "type": "trace",
        "origin_function": _origin(event, payload),
        "status": status,
        "method_params": _method_params(event, payload),
        "method_return_value": _return_value(event, payload),
        "memory_query": _memory_query(event, payload),
        "memory_context": _memory_context(payload),
        "error_message": error_message,
        "generate_feedback_with_llm": False,
    }


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


async def _native_user() -> Any:
    from cognee.modules.users.methods import get_default_user

    return await get_default_user()


async def _ensure_native_cognee_ready() -> None:
    from cognee.modules.engine.operations.setup import setup

    await setup()


async def _ensure_native_dataset_ready(dataset: str, user: Any) -> None:
    from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
        resolve_authorized_user_datasets,
    )

    await resolve_authorized_user_datasets(dataset, user=user)


async def _remember_entry_native(
    dataset: str,
    session_id: str,
    entry: dict[str, Any],
) -> Any:
    import cognee
    from cognee.memory import QAEntry, TraceEntry

    await _ensure_native_cognee_ready()
    user = await _native_user()
    await _ensure_native_dataset_ready(dataset, user)
    if entry.get("type") == "qa":
        typed_entry = QAEntry(**entry)
    elif entry.get("type") == "trace":
        typed_entry = TraceEntry(**entry)
    else:
        raise ValueError(f"Unsupported Cognee memory entry type: {entry.get('type')}")

    return await cognee.remember(
        typed_entry,
        dataset_name=dataset,
        session_id=session_id,
        self_improvement=False,
        user=user,
    )


async def _recall_native(dataset: str, session_id: str, prompt: str) -> dict[str, Any]:
    import cognee
    from cognee.modules.search.types import SearchType

    empty = {"context": "", "counts": _zero_recall_counts()}
    if not prompt or len(prompt) < 5 or _env_bool("COGNEE_CODEX_RECALL_DISABLED"):
        return empty

    await _ensure_native_cognee_ready()
    user = await _native_user()
    await _ensure_native_dataset_ready(dataset, user)
    raw_items: list[Any] = []
    for scope in _recall_scope():
        query_type = SearchType.GRAPH_COMPLETION if scope == "graph" else None
        try:
            part = await cognee.recall(
                prompt,
                query_type=query_type,
                session_id=session_id,
                datasets=[dataset],
                top_k=_recall_top_k(),
                scope=[scope],
                only_context=True,
                user=user,
            )
            raw_items.extend(part or [])
        except Exception as exc:
            _log("native_recall_scope_error", {"scope": scope, "error": str(exc)[:300]})

    normalized: list[Any] = []
    for item in raw_items:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if isinstance(item, dict) and item.get("source") == "graph":
            item["source"] = "graph_context"
        normalized.append(item)

    counts = _zero_recall_counts()
    buckets = {source: [] for source in RECALL_SOURCES}
    for item in normalized:
        if not _recall_item_text(item).strip():
            continue
        source = _recall_source(item)
        counts[source] += 1
        buckets[source].append(item)

    if not any(counts.values()):
        _log("native_recall_empty", {"session_id": session_id, "counts": counts})
        return {"context": "", "counts": counts}

    context = _build_recall_context(buckets, counts)
    if not context:
        _log("native_recall_empty", {"session_id": session_id, "counts": counts})
        return {"context": "", "counts": counts}

    _log("native_recall_hit", {"session_id": session_id, "items": len(normalized), "counts": counts})
    return {"context": context, "counts": counts}


async def _improve_native(dataset: str, session_id: str) -> Any:
    import cognee

    await _ensure_native_cognee_ready()
    user = await _native_user()
    await _ensure_native_dataset_ready(dataset, user)
    return await cognee.improve(
        dataset=dataset,
        session_ids=[session_id],
        run_in_background=_env_bool("COGNEE_CODEX_IMPROVE_BACKGROUND", True),
        user=user,
    )


def _native_cognee_available() -> bool:
    try:
        with redirect_stdout(sys.stderr):
            __import__("cognee")
        return True
    except Exception as exc:
        _log("native_cognee_unavailable", {"error": str(exc)[:300]})
        return False


def _use_native_backend() -> bool:
    mode = _backend_mode()
    if mode == "native":
        return True
    if mode == "http":
        return False
    if _explicit_http_connection_configured():
        return False
    return _native_cognee_available()


def _post_to_cognee_native(
    dataset: str,
    session_id: str,
    entry: dict[str, Any],
) -> tuple[int, str]:
    with redirect_stdout(sys.stderr):
        result = _run_async(_remember_entry_native(dataset, session_id, entry))
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {"result": str(result)}
    return 200, json.dumps(payload, default=str)


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    return headers


def _post_json(
    service_url: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    timeout: float,
    read_limit: int = 2048,
) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{service_url}{path}",
        data=body,
        headers=_headers(api_key),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        text = response.read(read_limit).decode("utf-8", errors="replace")
        return response.status, text


def _post_to_cognee(
    service_url: str,
    api_key: str,
    dataset: str,
    session_id: str,
    entry: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    return _post_json(
        service_url,
        api_key,
        "/api/v1/remember/entry",
        {"dataset_name": dataset, "session_id": session_id, "entry": entry},
        timeout,
    )


def _ensure_dataset(service_url: str, api_key: str, dataset: str, timeout: float) -> bool:
    """Create the dataset if needed so session records can attach to it."""
    if not dataset:
        return False
    try:
        status, text = _post_json(
            service_url,
            api_key,
            "/api/v1/datasets",
            {"name": dataset},
            timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        _log("dataset_ensure_http_error", {"dataset": dataset, "status": exc.code, "body": body})
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log("dataset_ensure_connection_error", {"dataset": dataset, "error": str(exc)[:300]})
        return False
    except Exception as exc:
        _log("dataset_ensure_error", {"dataset": dataset, "error": str(exc)[:300]})
        return False

    if 200 <= status < 300:
        _log("dataset_ensured", {"dataset": dataset, "status": status, "response": text[:300]})
        return True

    _log("dataset_ensure_http_status", {"dataset": dataset, "status": status, "body": text[:300]})
    return False


def _post_improve_to_cognee(
    service_url: str,
    api_key: str,
    dataset: str,
    session_id: str,
    timeout: float,
) -> tuple[int, str]:
    return _post_json(
        service_url,
        api_key,
        "/api/v1/improve",
        {
            "dataset_name": dataset,
            "session_ids": [session_id],
            "run_in_background": _env_bool("COGNEE_CODEX_IMPROVE_BACKGROUND", True),
        },
        timeout,
    )


def _maybe_fire_auto_improve(
    service_url: str,
    api_key: str,
    dataset: str,
    session_id: str,
    event: str,
) -> None:
    count, reason = _bump_auto_improve_counter(session_id, event)
    if not reason:
        return

    timeout = _improve_timeout_seconds()
    _ensure_dataset(service_url, api_key, dataset, timeout)
    try:
        status, text = _post_improve_to_cognee(
            service_url,
            api_key,
            dataset,
            session_id,
            timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        _log(
            "auto_improve_http_error",
            {
                "dataset": dataset,
                "session_id": session_id,
                "reason": reason,
                "status": exc.code,
                "body": body,
            },
        )
        return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(
            "auto_improve_connection_error",
            {
                "dataset": dataset,
                "session_id": session_id,
                "reason": reason,
                "error": str(exc)[:300],
            },
        )
        return
    except Exception as exc:
        _log(
            "auto_improve_error",
            {
                "dataset": dataset,
                "session_id": session_id,
                "reason": reason,
                "error": str(exc)[:300],
            },
        )
        return

    log_event = "auto_improve_fired" if 200 <= status < 300 else "auto_improve_http_status"
    _log(
        log_event,
        {
            "dataset": dataset,
            "session_id": session_id,
            "event": event,
            "count": count,
            "reason": reason,
            "status": status,
            "response": text[:500],
        },
    )


def _maybe_fire_auto_improve_native(dataset: str, session_id: str, event: str) -> None:
    count, reason = _bump_auto_improve_counter(session_id, event)
    if not reason:
        return

    try:
        with redirect_stdout(sys.stderr):
            result = _run_async(_improve_native(dataset, session_id))
    except Exception as exc:
        _log(
            "native_auto_improve_error",
            {
                "dataset": dataset,
                "session_id": session_id,
                "reason": reason,
                "error": str(exc)[:300],
            },
        )
        return

    _log(
        "native_auto_improve_fired",
        {
            "dataset": dataset,
            "session_id": session_id,
            "event": event,
            "count": count,
            "reason": reason,
            "response": _truncate(result, 500),
        },
    )


def _timeout_seconds() -> float:
    raw = os.environ.get("COGNEE_CODEX_HOOK_TIMEOUT", "")
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(0.2, min(value, 10.0))


def _recall_timeout_seconds() -> float:
    raw = os.environ.get("COGNEE_CODEX_RECALL_TIMEOUT", "")
    try:
        value = float(raw) if raw else DEFAULT_RECALL_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_RECALL_TIMEOUT_SECONDS
    return max(0.2, min(value, 5.0))


def _improve_timeout_seconds() -> float:
    raw = os.environ.get("COGNEE_CODEX_IMPROVE_TIMEOUT", "")
    try:
        value = float(raw) if raw else DEFAULT_IMPROVE_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_IMPROVE_TIMEOUT_SECONDS
    return max(0.2, min(value, 4.0))


def _recall_top_k() -> int:
    raw = os.environ.get("COGNEE_CODEX_RECALL_TOP_K", "")
    try:
        value = int(raw) if raw else DEFAULT_RECALL_TOP_K
    except ValueError:
        value = DEFAULT_RECALL_TOP_K
    return max(1, min(value, 20))


def _recall_scope() -> list[str]:
    raw = os.environ.get("COGNEE_CODEX_RECALL_SCOPE", "")
    if not raw:
        return list(DEFAULT_RECALL_SCOPE)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(DEFAULT_RECALL_SCOPE)


def _flatten_recall_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        out: list[Any] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                out.extend(item["results"])
            else:
                out.append(item)
        return out
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data["results"]
        return [data]
    return []


def _recall_source(item: Any) -> str:
    if not isinstance(item, dict):
        return "session"

    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source = item.get("source") or metadata.get("source") or raw.get("source") or item.get("kind")
    source = str(source or "session")

    if source == "graph":
        return "graph_context"
    if source in RECALL_SOURCES:
        return source
    if source.startswith("graph"):
        return "graph_context"
    return "session"


def _first_text(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def _clean_preview(value: Any, max_bytes: int) -> str:
    text = _truncate(value, max_bytes)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _parse_qa_text(text: Any) -> tuple[str, str]:
    if not isinstance(text, str):
        return "", ""
    match = re.search(r"\bQ:\s*(.*?)\s*\bA:\s*(.*)", text, re.DOTALL)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _raw_dict(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("raw") if isinstance(item.get("raw"), dict) else {}


def _session_preview(item: dict[str, Any]) -> str:
    raw = _raw_dict(item)
    q = _first_text(item.get("question"), raw.get("question"))
    a = _first_text(item.get("answer"), raw.get("answer"))

    text = _first_text(
        item.get("text"),
        item.get("content"),
        raw.get("text"),
        raw.get("content"),
    )
    if not q and not a:
        q, a = _parse_qa_text(text)

    parts = []
    if q:
        parts.append(f"User: {_clean_preview(q, 220)}")
    if a:
        parts.append(f"Assistant: {_clean_preview(a, MAX_SESSION_PREVIEW_BYTES)}")
    if parts:
        return " | ".join(parts)

    return _clean_preview(text or item, MAX_SESSION_PREVIEW_BYTES)


def _looks_like_noisy_trace(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    compact = text.lstrip().lower()
    return (
        compact.startswith("---\nname:")
        or "use this skill when" in compact[:800]
        or "before the first browser action" in compact[:800]
    )


def _trace_preview(item: dict[str, Any]) -> str:
    raw = _raw_dict(item)
    params = _first_text(item.get("method_params"), raw.get("method_params"))
    if not isinstance(params, dict):
        params = {}
    tool_input = params.get("tool_input") if isinstance(params.get("tool_input"), dict) else {}

    origin = _first_text(
        item.get("origin_function"),
        raw.get("origin_function"),
        item.get("tool_name"),
        raw.get("tool_name"),
        params.get("tool_name"),
        "trace",
    )
    status = _first_text(item.get("status"), raw.get("status"))
    query = _first_text(
        item.get("memory_query"),
        raw.get("memory_query"),
        tool_input.get("command"),
        tool_input.get("cmd"),
    )
    detail = _first_text(
        query,
        item.get("session_feedback"),
        raw.get("session_feedback"),
        item.get("error_message"),
        raw.get("error_message"),
        item.get("method_return_value"),
        raw.get("method_return_value"),
        item.get("text"),
        raw.get("text"),
    )
    if _looks_like_noisy_trace(detail):
        return ""

    label = str(origin)
    if status:
        label = f"{label} ({status})"
    if detail:
        return f"{label}: {_clean_preview(detail, MAX_TRACE_PREVIEW_BYTES)}"
    return label


def _graph_preview(item: dict[str, Any]) -> str:
    raw = _raw_dict(item)
    text = _first_text(
        item.get("content"),
        item.get("text"),
        raw.get("content"),
        raw.get("text"),
        raw.get("value"),
    )
    return _clean_preview(text, MAX_GRAPH_PREVIEW_BYTES)


def _recall_item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return _clean_preview(item, MAX_SESSION_PREVIEW_BYTES)

    source = _recall_source(item)
    if source == "trace":
        return _trace_preview(item)
    if source == "graph_context":
        return _graph_preview(item)
    return _session_preview(item)


def _append_recall_section(
    lines: list[str],
    title: str,
    items: list[Any],
    *,
    source: str,
) -> int:
    limit = RECALL_SECTION_LIMITS[source]
    rendered: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _recall_item_text(item).strip()
        if not text or text in seen:
            continue
        rendered.append(text)
        seen.add(text)
        if len(rendered) >= limit:
            break

    if not rendered:
        return 0

    lines.extend(["", f"{title}:"])
    for index, text in enumerate(rendered, start=1):
        lines.append(f"{index}. {text}")

    hidden = max(0, len(items) - len(rendered))
    if hidden:
        label = "match" if hidden == 1 else "matches"
        lines.append(f"... {hidden} more {source.replace('_', ' ')} {label} not shown.")
    return len(rendered)


def _build_recall_context(buckets: dict[str, list[Any]], counts: dict[str, int]) -> str:
    lines = ["Relevant Cognee memory for this Codex session:"]
    displayed = {
        "session": _append_recall_section(
            lines, "Session matches", buckets["session"], source="session"
        ),
        "graph_context": _append_recall_section(
            lines,
            "Knowledge graph matches",
            buckets["graph_context"],
            source="graph_context",
        ),
        "trace": _append_recall_section(
            lines, "Tool trace matches", buckets["trace"], source="trace"
        ),
    }

    for source in RECALL_SOURCES:
        if counts[source] and not displayed[source]:
            lines.extend(
                [
                    "",
                    (
                        f"{source.replace('_', ' ').title()} matches: "
                        f"{counts[source]} found, omitted from preview to keep the hook readable."
                    ),
                ]
            )

    return _truncate("\n".join(lines).strip(), MAX_CONTEXT_BYTES)


def _recall_context(
    service_url: str,
    api_key: str,
    session_id: str,
    prompt: str,
) -> dict[str, Any]:
    empty = {"context": "", "counts": _zero_recall_counts()}
    if not prompt or len(prompt) < 5 or _env_bool("COGNEE_CODEX_RECALL_DISABLED"):
        return empty

    payload = {
        "query": prompt,
        "search_type": "GRAPH_COMPLETION",
        "session_id": session_id,
        "scope": _recall_scope(),
        "only_context": True,
        "top_k": _recall_top_k(),
    }

    try:
        status, text = _post_json(
            service_url,
            api_key,
            "/api/v1/recall",
            payload,
            _recall_timeout_seconds(),
            read_limit=MAX_RECALL_RESPONSE_BYTES,
        )
        if not (200 <= status < 300):
            _log("recall_http_status", {"status": status, "body": text[:300]})
            return empty
        data = json.loads(text) if text.strip() else []
    except Exception as exc:
        _log("recall_error", {"error": str(exc)[:300]})
        return empty

    raw_items = _flatten_recall_items(data)
    counts = _zero_recall_counts()
    buckets = {source: [] for source in RECALL_SOURCES}
    for item in raw_items:
        if not _recall_item_text(item).strip():
            continue
        source = _recall_source(item)
        counts[source] += 1
        buckets[source].append(item)

    if not any(counts.values()):
        _log("recall_empty", {"session_id": session_id, "counts": counts})
        return {"context": "", "counts": counts}

    context = _build_recall_context(buckets, counts)
    if not context:
        _log("recall_empty", {"session_id": session_id, "counts": counts})
        return {"context": "", "counts": counts}

    _log("recall_hit", {"session_id": session_id, "items": len(raw_items), "counts": counts})
    return {"context": context, "counts": counts}


def _status_line(recall_counts: dict[str, int], saves_last_turn: dict[str, int]) -> str:
    return (
        "Cognee memory: recall "
        f"{recall_counts.get('session', 0)} session / "
        f"{recall_counts.get('trace', 0)} trace / "
        f"{recall_counts.get('graph_context', 0)} graph"
        "; saved last turn "
        f"{saves_last_turn.get('prompt', 0)} prompt / "
        f"{saves_last_turn.get('trace', 0)} trace / "
        f"{saves_last_turn.get('answer', 0)} answer"
    )


def _write_recall_state(
    session_id: str,
    prompt: str,
    recall_counts: dict[str, int],
    saves_last_turn: dict[str, int],
    status_line: str,
    recall_context: str,
) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": _now(),
            "session_id": session_id,
            "prompt": _truncate(prompt, 1200),
            "hits": recall_counts,
            "saves_last_turn": saves_last_turn,
            "status_line": status_line,
        }
        LAST_RECALL_PATH.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        audit = dict(payload)
        audit["context"] = recall_context
        with RECALL_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, sort_keys=True) + "\n")
    except Exception:
        pass


def _maybe_stdout_for_codex(
    event: str,
    recall_context: str = "",
    status_line: str = "",
) -> None:
    if event == "UserPromptSubmit":
        additional_context = status_line
        if recall_context:
            additional_context = f"{status_line}\n\n{recall_context}"
        elif status_line:
            additional_context = f"{status_line}\n\n(no memory matches for this prompt)"

        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": additional_context,
                    }
                }
            )
        )
        return

    if event == "Stop":
        print("{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=None)
    args = parser.parse_args()

    started = time.monotonic()
    payload = _read_payload()
    event = _event_name(args.event, payload)
    recall_context = ""
    recall_counts = _zero_recall_counts()
    saves_last_turn = _zero_save_counts()
    status_line = ""

    try:
        session_id = _session_id(payload)
        dataset = _dataset_name()
        use_native = _use_native_backend()
        if not use_native:
            service_url, api_key = _load_connection()
            if event in {"SessionStart", "UserPromptSubmit", "Stop"}:
                _ensure_dataset(service_url, api_key, dataset, _timeout_seconds())
        if event == "UserPromptSubmit":
            saves_last_turn = _read_and_reset_save_counter(session_id)
            _remember_pending_prompt(payload)
            if use_native:
                with redirect_stdout(sys.stderr):
                    recall_result = _run_async(
                        _recall_native(dataset, session_id, payload.get("prompt", ""))
                    )
            else:
                recall_result = _recall_context(
                    service_url, api_key, session_id, payload.get("prompt", "")
                )
            recall_context = str(recall_result.get("context", ""))
            recall_counts = dict(recall_result.get("counts", _zero_recall_counts()))
            status_line = _status_line(recall_counts, saves_last_turn)
            _write_recall_state(
                session_id,
                str(payload.get("prompt", "")),
                recall_counts,
                saves_last_turn,
                status_line,
                recall_context,
            )
        entry = _entry(event, payload)
        if use_native:
            status, response_text = _post_to_cognee_native(dataset, session_id, entry)
        else:
            status, response_text = _post_to_cognee(
                service_url,
                api_key,
                dataset,
                session_id,
                entry,
                _timeout_seconds(),
            )
        _log(
            "stored_native" if use_native else "stored",
            {
                "event": event,
                "status": status,
                "session_id": session_id,
                "dataset": dataset,
                "backend": "native" if use_native else "http",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "response": response_text,
            },
        )
        if 200 <= status < 300:
            _bump_save_counter(session_id, _save_kind(event))
            if use_native:
                _maybe_fire_auto_improve_native(dataset, session_id, event)
            else:
                _maybe_fire_auto_improve(service_url, api_key, dataset, session_id, event)
        _debug(f"stored {event} for {session_id} ({status})")
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        _log("http_error", {"event": event, "status": exc.code, "body": body})
        _debug(f"Cognee rejected {event}: HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log("connection_error", {"event": event, "error": str(exc)[:300]})
        _debug(f"Cognee unavailable for {event}: {exc}")
    except Exception as exc:
        _log("unexpected_error", {"event": event, "error": str(exc)[:300]})
        _debug(f"failed to store {event}: {exc}")

    if event == "UserPromptSubmit" and not status_line:
        status_line = _status_line(recall_counts, saves_last_turn)
    _maybe_stdout_for_codex(event, recall_context, status_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
