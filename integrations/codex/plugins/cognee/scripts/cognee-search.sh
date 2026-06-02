#!/usr/bin/env bash
# Search Cognee's memory (session or permanent graph).
#
# Usage:
#   cognee-search.sh <query> [top_k] [--session | --graph]
#
# --session: search session cache only
# --graph:   search permanent knowledge graph only
# No flag:   search session first, then graph if empty
#
# Configuration:
#   Resolves session ID and dataset from Cognee endpoints using API auth.
#   Falls back to env vars when endpoint lookup is unavailable.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/codex"
SESSION_KEY="${COGNEE_SESSION_KEY:-}"
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" "${SESSION_KEY}" 2>/dev/null || true
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

plugin_dir = pathlib.Path(sys.argv[1])
session_key = (sys.argv[2] or "").strip()
import os
service_url = (os.environ.get("COGNEE_SERVICE_URL") or os.environ.get("COGNEE_LOCAL_API_URL") or "").strip()
api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
agent_name = (os.environ.get("COGNEE_AGENT_NAME") or "").strip()

if not (service_url and api_key):
    cache_path = plugin_dir / "agent_keys.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
            chosen = None
            if isinstance(entries, dict):
                if agent_name:
                    for entry in entries.values():
                        if isinstance(entry, dict) and str(entry.get("agent_name") or "").strip() == agent_name:
                            chosen = entry
                            break
                if chosen is None:
                    latest_ts = ""
                    for entry in entries.values():
                        if not isinstance(entry, dict):
                            continue
                        ts = str(entry.get("last_used_at") or entry.get("created_at") or "")
                        if ts >= latest_ts:
                            latest_ts = ts
                            chosen = entry
            if isinstance(chosen, dict):
                service_url = service_url or str(chosen.get("service_url") or "").strip()
                api_key = api_key or str(chosen.get("api_key") or "").strip()
        except Exception:
            pass

session_id = ""
dataset = ""
if service_url and api_key:
    try:
        query = ""
        if session_key:
            query = "?agent_session_name=" + urllib.parse.quote(session_key, safe="")
        req = urllib.request.Request(
            service_url.rstrip("/") + "/api/v1/agents/connections/me" + query,
            headers={"X-Api-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(payload, dict):
            agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
            if isinstance(agent, dict):
                session_id = str(agent.get("session_id") or "").strip()
                datasets = agent.get("datasets") if isinstance(agent.get("datasets"), list) else []
                for item in datasets:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            dataset = name
                            break
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        pass

print(json.dumps({"session_id": session_id, "dataset": dataset}))
PY
)"

DATASET="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("dataset") or "").strip())
except Exception:
    pass
PY
)"
SESSION_ID="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("session_id") or "").strip())
except Exception:
    pass
PY
)"
[ -z "$DATASET" ] && DATASET="${COGNEE_PLUGIN_DATASET:-codex_sessions}"
[ -z "$SESSION_ID" ] && SESSION_ID="${COGNEE_SESSION_ID:-codex_session}"

QUERY="${1:-}"
TOP_K="${2:-5}"
MODE="auto"

# Parse flags from any position
for arg in "$@"; do
    case "$arg" in
        --session) MODE="session" ;;
        --graph)   MODE="graph" ;;
    esac
done

if [ -z "$QUERY" ]; then
    echo "Error: no query provided" >&2
    exit 1
fi

if [ "$MODE" = "graph" ]; then
    cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null
elif [ "$MODE" = "session" ]; then
    cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null
else
    # Auto: try session first, fall back to graph
    RESULT=$(cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null)
    if [ -n "$RESULT" ] && [ "$RESULT" != "[]" ]; then
        echo "$RESULT"
    else
        cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null
    fi
fi
