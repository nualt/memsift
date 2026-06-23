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
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" 2>/dev/null || true
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

plugin_dir = pathlib.Path(sys.argv[1])
import os
service_url = (os.environ.get("COGNEE_BASE_URL") or os.environ.get("COGNEE_LOCAL_API_URL") or "http://localhost:8011").strip()
api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
agent_name = (os.environ.get("COGNEE_AGENT_NAME") or "").strip()
if agent_name:
    if agent_name.endswith("@cognee.agent"):
        agent_name = agent_name[: -len("@cognee.agent")]
    if not agent_name.endswith("_codex"):
        agent_name = f"{agent_name}_codex"

if not api_key and service_url and agent_name:
    cache_path = plugin_dir / "agent_keys.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
            if isinstance(entries, dict):
                normalized_url = service_url.rstrip("/")
                key = f"{normalized_url}::{agent_name}"
                chosen = entries.get(key)
                if isinstance(chosen, dict):
                    api_key = str(chosen.get("api_key") or "").strip()
                else:
                    for entry in entries.values():
                        if not isinstance(entry, dict):
                            continue
                        name = str(entry.get("agent_name") or "").strip()
                        url = str(entry.get("base_url") or "").strip().rstrip("/")
                        if name == agent_name and url == normalized_url:
                            api_key = str(entry.get("api_key") or "").strip()
                            break
        except Exception:
            pass

session_id = ""
dataset = ""
if service_url and api_key:
    try:
        query = ""
        session_key = (os.environ.get("COGNEE_SESSION_KEY") or "").strip()
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

print(json.dumps({"session_id": session_id, "dataset": dataset, "service_url": service_url, "api_key": api_key}))
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
SERVICE_URL="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("service_url") or "").strip())
except Exception:
    pass
PY
)"
API_KEY="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("api_key") or "").strip())
except Exception:
    pass
PY
)"
[ -z "$DATASET" ] && DATASET="${COGNEE_PLUGIN_DATASET:-codex_sessions}"
[ -z "$SESSION_ID" ] && SESSION_ID="${COGNEE_SESSION_ID:-codex_session}"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"

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

# Search scope from MODE
case "$MODE" in
    session) SCOPE='["session"]' ;;
    graph)   SCOPE='["graph"]' ;;
    *)       SCOPE='["session", "graph"]' ;;
esac

# Server-first: the running server (/api/v1/recall) is the source of truth.
# An empty result from the server is authoritative; fall back to cognee-cli ONLY
# when the server is genuinely unreachable — never to "rescue" an empty result.
RECALL_JSON="$(python3 - "$SERVICE_URL" "$API_KEY" "$QUERY" "$SESSION_ID" "$DATASET" "$TOP_K" "$SCOPE" <<'PY' 2>/dev/null || true
import json, sys, urllib.request, urllib.error
a = (sys.argv + [""] * 7)
service_url, api_key, query, session_id, dataset, top_k, scope = a[1], a[2], a[3], a[4], a[5], a[6], a[7]
url = service_url.rstrip("/") + "/api/v1/recall"
body = {"query": query, "top_k": int(top_k or 5), "only_context": True, "scope": json.loads(scope or '"auto"')}
if session_id:
    body["session_id"] = session_id
# No `datasets` restriction: search ALL the user's authorized datasets (matches the
# auto-recall path that ground-truths content). Restricting to the plugin's default
# dataset is exactly what produced false "not found" verdicts when the content lived
# in another dataset.
headers = {"Content-Type": "application/json"}
if api_key:
    headers["X-Api-Key"] = api_key
req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        data = json.loads(resp.read().decode("utf-8") or "[]")
    print(json.dumps(data if isinstance(data, list) else [data]))
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        sys.stderr.write("[cognee-search] auth failed (HTTP %s) — check COGNEE_API_KEY\n" % e.code)
    else:
        sys.stderr.write("[cognee-search] server HTTP %s for /api/v1/recall (treating as no results)\n" % e.code)
    print("[]")
except Exception as e:
    sys.stderr.write("[cognee-search] server unreachable at %s: %s\n" % (service_url, str(e)[:160]))
    print("UNREACHABLE")
PY
)"

if [ -n "$RECALL_JSON" ] && [ "$RECALL_JSON" != "UNREACHABLE" ]; then
    # Server answered — authoritative, even if the result is empty.
    printf '%s\n' "$RECALL_JSON"
else
    echo "[cognee-search] falling back to cognee-cli (degraded — empty CLI output is NOT proof of absence; ground-truth via: curl -X POST \"\$COGNEE_BASE_URL/api/v1/recall\")" >&2
    if [ "$MODE" = "graph" ]; then
        cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null || true
    elif [ "$MODE" = "session" ]; then
        cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null || true
    else
        RESULT=$(cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null || true)
        if [ -n "$RESULT" ] && [ "$RESULT" != "[]" ]; then
            echo "$RESULT"
        else
            cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null || true
        fi
    fi
fi
