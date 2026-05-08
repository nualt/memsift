#!/usr/bin/env python3
"""Search session + trace + graph-context for context relevant to the user's prompt.

Runs on the UserPromptSubmit hook. Calls ``cognee.recall`` with
``scope=["session","trace","graph_context"]`` so every layer the
SessionManager holds (QA entries, agent trace steps, and the distilled
graph-knowledge snapshot from ``improve()``) flows back into Claude's
context.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    hook_log,
    load_resolved,
    notify,
    read_and_reset_save_counter,
    recall_via_http,
    resolve_user,
)
from config import ensure_cognee_ready, get_session_id, is_cloud_mode, load_config

TOP_K = 5
TRUNCATE_ANSWER = 500
TRUNCATE_RETURN = 400
TRUNCATE_GRAPH_CTX = 1500


def _load_session_id() -> str:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    if not session_id:
        config = load_config()
        session_id = get_session_id(config)
    return session_id


def _load_user_id() -> str:
    return load_resolved().get("user_id", "")


def _format_entry(entry: dict) -> str:
    """Format a single recall result according to its _source tag."""
    source = entry.get("source", "")

    if source == "graph_context":
        # graph_context entries carry `content`; graph_completion results
        # (folded in from scope=graph) carry `text`. Try both.
        content = str(entry.get("content", "") or entry.get("text", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[graph-snapshot]\n{content}"

    if source == "trace":
        origin = entry.get("origin_function", "?")
        status = entry.get("status", "")
        feedback = entry.get("session_feedback", "")
        mrv = entry.get("method_return_value", "")
        if isinstance(mrv, (dict, list)):
            mrv = json.dumps(mrv, default=str)
        mrv = str(mrv)[:TRUNCATE_RETURN]
        parts = [f"[trace] {origin} — {status}"]
        if feedback:
            parts.append(f"  feedback: {feedback}")
        if mrv:
            parts.append(f"  output: {mrv}")
        return "\n".join(parts)

    # session (QA) or generic
    q = entry.get("question", "")
    a = entry.get("answer", "")
    t = entry.get("time", "")
    lines = []
    if q:
        lines.append(f"[{t}] Q: {q}")
    if a:
        a_short = a[:TRUNCATE_ANSWER] + "..." if len(a) > TRUNCATE_ANSWER else a
        lines.append(f"A: {a_short}")
    return "\n".join(lines)


def _has_entry_content(entry: dict) -> bool:
    """Return True when a recall entry has useful content to inject."""
    source = entry.get("source", "")
    if source == "graph_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "trace":
        fields = ("origin_function", "status", "session_feedback", "method_return_value")
    else:
        fields = ("question", "answer")
    return any(str(entry.get(field, "") or "").strip() for field in fields)


async def _run(prompt: str, out_stream=None):
    config = load_config()
    await ensure_cognee_ready(config)

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return

    saves_last_turn = read_and_reset_save_counter(session_id)

    # Run scopes independently: a failure in one (e.g. graph search hitting an
    # empty/locked Ladybug DB) must not discard hits already collected from the
    # others. cognee.recall loops over scopes and re-raises on the first failure,
    # so we call it once per scope and collect whatever succeeds.
    results: list = []
    scope_specs = [
        (["session"], None),
        (["trace"], None),
        (["graph_context"], None),
        (["graph"], "GRAPH_COMPLETION"),
    ]
    cloud_mode = is_cloud_mode(config)
    if not cloud_mode:
        import cognee
        from cognee.modules.search.types import SearchType

        user = await resolve_user(_load_user_id())

    for scope_list, qtype in scope_specs:
        try:
            if cloud_mode:
                part = recall_via_http(
                    prompt,
                    session_id=session_id,
                    top_k=TOP_K,
                    scope=scope_list,
                    only_context=True,
                    search_type=qtype,
                )
            else:
                query_type = SearchType.GRAPH_COMPLETION if qtype == "GRAPH_COMPLETION" else None
                part = await cognee.recall(
                    prompt,
                    session_id=session_id,
                    top_k=TOP_K,
                    scope=scope_list,
                    only_context=True,
                    query_type=query_type,
                    user=user,
                )
            if part:
                results.extend(part)
        except Exception as exc:
            hook_log("recall_error", {"scope": scope_list, "error": str(exc)[:200]})

    # Bucket results by _source for human-readable output.
    # Local SDK mode returns Pydantic models (ResponseQAEntry, etc.); cloud
    # mode returns plain dicts via HTTP. Normalize to dicts here.
    by_source: dict[str, list] = {"session": [], "trace": [], "graph_context": []}
    for r in results or []:
        if hasattr(r, "model_dump"):
            r = r.model_dump()
        if not isinstance(r, dict):
            continue
        src = r.get("source", "session")
        # Fold scope=graph (GRAPH_COMPLETION) results into the graph_context
        # bucket so the displayed `g` counter reflects what was retrieved.
        if src == "graph":
            r["source"] = "graph_context"
            src = "graph_context"
        if not _has_entry_content(r):
            continue
        by_source.setdefault(src, []).append(r)

    counts = {k: len(v) for k, v in by_source.items()}
    total = sum(counts.values())

    # Write last-turn counts so the status line script can render them.
    # Best-effort; failure here must not break the hook output.
    try:
        from pathlib import Path as _Path

        _state = _Path.home() / ".cognee-plugin" / "last_recall.json"
        _state.parent.mkdir(parents=True, exist_ok=True)
        _state.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "ts": __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat(timespec="seconds"),
                    "hits": counts,
                    "saves_last_turn": saves_last_turn,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Build a one-line visibility header so the user (via the assistant's
    # context) can tell that memory fired on this turn — both what it
    # recalled right now and what the previous turn persisted.
    recall_tag = (
        f"🔍 cognee recall: {counts['session']} session / "
        f"{counts['trace']} trace / {counts['graph_context']} graph-ctx hits"
    )
    saves_tag = (
        f"💾 saves last turn: {saves_last_turn['prompt']} prompt / "
        f"{saves_last_turn['trace']} trace / {saves_last_turn['answer']} answer"
    )
    header = f"{recall_tag}    |    {saves_tag}"

    section_lines = []
    if by_source.get("graph_context"):
        section_lines.append("=== Knowledge graph snapshot ===")
        for e in by_source["graph_context"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("trace"):
        section_lines.append("=== Prior agent trace ===")
        for e in by_source["trace"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("session"):
        section_lines.append("=== Prior session turns ===")
        for e in by_source["session"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")

    if total > 0:
        context = (
            f"{header}\n\nRelevant context from this session's memory:\n\n"
            + "\n".join(section_lines).strip()
        )
        hook_log("context_lookup_hit", {"counts": counts, "saves_last_turn": saves_last_turn})
        notify(f"injected context ({counts}); saves last turn {saves_last_turn}")
    else:
        context = f"{header}\n\n(no memory matches for this prompt)"
        hook_log("context_lookup_empty", {"saves_last_turn": saves_last_turn})
        notify(f"no recall matches; saves last turn {saves_last_turn}")

    # Audit log: persist the full injected context per turn. The Claude Code
    # JSONL transcript does not preserve UserPromptSubmit additionalContext,
    # so this file is the source of truth for "what did the plugin give
    # Claude on prompt X."
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from pathlib import Path as _Path

        _audit = _Path.home() / ".cognee-plugin" / "recall-audit.log"
        _audit.parent.mkdir(parents=True, exist_ok=True)
        with _audit.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                        "session_id": session_id,
                        "prompt": prompt,
                        "hits": counts,
                        "context": context,
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
            # Surfaces the one-line header to the user's terminal (UI),
            # so they can see that memory fired even though the full
            # context only goes to the model via additionalContext.
            "systemMessage": header,
        }
    }
    print(json.dumps(output), file=out_stream or sys.stdout)


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    # Claude Code expects pure JSON on stdout. Some cognee codepaths (e.g.
    # serve registration) print human-facing banners to stdout, which would
    # otherwise contaminate the hook output and prevent systemMessage from
    # rendering in the user's terminal. Redirect stdout to stderr for the
    # cognee call, then write our JSON to the real stdout at the end.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        asyncio.run(_run(prompt, real_stdout))
    except Exception as exc:
        hook_log("context_lookup_exception", {"error": str(exc)[:200]})
    finally:
        sys.stdout = real_stdout


if __name__ == "__main__":
    main()
