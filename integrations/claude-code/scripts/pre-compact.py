#!/usr/bin/env python3
"""Build a memory anchor before context window compaction.

Runs on the PreCompact hook (triggered when the context window is full
or when the user manually compacts). Fetches session + graph context
and outputs a markdown block that the compactor preserves.

This ensures key knowledge survives context resets.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from config import load_config, get_session_id, get_dataset


_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
_MIN_WORD_LEN = 2
_SESSION_TOP_K = 5
_GRAPH_TOP_K = 3


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


async def _get_session_entries(session_id: str, cached_user_id: str = "") -> list:
    """Fetch recent session entries from cache engine (lightweight)."""
    try:
        from cognee.infrastructure.databases.cache.get_cache_engine import get_cache_engine
        from cognee.modules.users.methods import get_default_user

        user = await get_default_user()
        user_id = str(user.id) if hasattr(user, "id") else ""
        if not user_id:
            return []

        cache_engine = get_cache_engine()
        if cache_engine is None:
            return []

        entries = await cache_engine.get_all_qa_entries(user_id, session_id)
        return list(entries)[-_SESSION_TOP_K:] if entries else []
    except Exception:
        return []


async def _get_graph_context(query: str, dataset: str) -> list:
    """Fetch relevant graph context via recall (heavier, but worth it before compaction)."""
    try:
        import cognee
        results = await cognee.recall(
            query_text=query,
            datasets=[dataset],
            top_k=_GRAPH_TOP_K,
            auto_route=True,
        )
        return results if results else []
    except Exception:
        return []


def _format_anchor(session_entries: list, graph_results: list) -> str:
    """Format the memory anchor markdown block."""
    sections = []

    if session_entries:
        lines = ["### Session Memory (recent actions)"]
        for entry in session_entries:
            if not isinstance(entry, dict):
                continue
            answer = entry.get("answer", "")
            if answer:
                # Truncate long entries
                short = answer[:300] + "..." if len(answer) > 300 else answer
                lines.append(f"- {short}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    if graph_results:
        lines = ["### Knowledge Graph (persistent memory)"]
        for r in graph_results:
            if isinstance(r, dict):
                text = r.get("answer", r.get("text", r.get("content", str(r))))
                source = r.get("_source", "graph")
                short = text[:300] + "..." if len(text) > 300 else text
                lines.append(f"- [{source}] {short}")
            else:
                lines.append(f"- {str(r)[:300]}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "## Cognee Memory Anchor\nPreserved context from session and knowledge graph:\n"
    return header + "\n\n".join(sections)


async def _run():
    session_id, dataset, user_id = _load_resolved()
    if not session_id:
        return

    # Fetch session entries (fast, cache-only)
    session_entries = await _get_session_entries(session_id, user_id)

    # Build a summary query from recent session entries for graph search
    query_parts = []
    for entry in session_entries[-3:]:
        if isinstance(entry, dict):
            answer = entry.get("answer", "")
            # Extract key words from recent entries
            words = {w for w in re.findall(r"\b\w+\b", answer.lower())
                     if len(w) >= _MIN_WORD_LEN}
            query_parts.extend(list(words)[:10])

    graph_results = []
    if query_parts:
        query = " ".join(query_parts[:20])
        graph_results = await _get_graph_context(query, dataset)

    anchor = _format_anchor(session_entries, graph_results)
    if anchor:
        print(anchor)


def main():
    # Read stdin (PreCompact payload)
    sys.stdin.read()

    try:
        asyncio.run(_run())
    except Exception:
        # Non-fatal: compaction proceeds without memory anchor
        pass


if __name__ == "__main__":
    main()
