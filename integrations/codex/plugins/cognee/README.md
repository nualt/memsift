# Cognee Codex Plugin

This plugin packages Cognee workflows for Codex. It lives alongside the Claude
Code integration in `cognee-integrations`, uses the Codex plugin manifest
format, and does not configure MCP.

When Codex hooks are enabled, the plugin automatically writes Codex session
events to Cognee session memory: session starts, user prompts, tool results,
and assistant stop messages. It also ensures the configured dataset exists and
periodically runs Cognee improve in the background so session cache entries are
bridged into the permanent graph. On each user prompt it recalls relevant
memory from the same Cognee backend and injects it into Codex context.

## Contents

- `.codex-plugin/plugin.json` - Codex plugin manifest.
- `hooks.json` - Codex lifecycle hooks for automatic session capture and
  recall injection.
- `skills/` - reusable Codex instructions for Cognee CLI workflows.
- `scripts/cognee-codex-hook.py` - hook handler that posts typed entries to
  `/api/v1/remember/entry`, recalls context from `/api/v1/recall`, and triggers
  `/api/v1/improve` to persist session cache into the graph.
- `scripts/cognee-cli.sh` - optional helper that runs `uv run cognee-cli` from a
  Cognee repository root.

## Local Install

From the Codex marketplace root, `integrations/codex`:

```bash
codex plugin marketplace add .
```

Restart Codex, open the plugin directory, select `Cognee Local Plugins`, and
install `Cognee`.

Codex hooks must be enabled in `~/.codex/config.toml`:

```toml
[features]
codex_hooks = true
```

The hook uses `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` when set, otherwise it
falls back to `~/.cognee/cloud_credentials.json`. If no URL is configured, it
uses `http://localhost:8000`. The default dataset is `codex_sessions`; override
it with `COGNEE_CODEX_DATASET`. The hook creates or reuses that dataset through
`/api/v1/datasets` so it appears in the Cognee UI and session rows can attach to
it. Automatic prompt recall searches
`session,trace,graph_context,graph` by default; override with
`COGNEE_CODEX_RECALL_SCOPE`. `graph_context` covers session-attached graph
snapshots, while `graph` searches the live Cognee graph and is counted in the
displayed graph bucket.

Typed session entries are first stored through `/api/v1/remember/entry`, which
is Cognee's session-cache path. To make those entries permanent, the hook also
posts `/api/v1/improve` with the active session ID. It fires on the first
assistant stop and then every 30 stored tool/assistant events by default. Set
`COGNEE_CODEX_AUTO_IMPROVE=false` to disable this, or
`COGNEE_CODEX_AUTO_IMPROVE_EVERY=<n>` to change the threshold. Improve runs in
background mode by default; set `COGNEE_CODEX_IMPROVE_BACKGROUND=false` only
when blocking graph sync is explicitly desired.

On every user prompt the hook prepends a status line to Codex
`hookSpecificOutput.additionalContext`, for example:

```text
Cognee memory: recall 1 session / 3 trace / 0 graph; saved last turn 1 prompt / 4 trace / 1 answer
```

The injected context is grouped into short session, graph, and tool-trace
sections so Codex's hook preview stays readable. The same status is written to
`~/.cognee/codex-last-recall.json`, and full prompt-level recall details are
appended to `~/.cognee/codex-recall-audit.log`. Per-session save counters live
in `~/.cognee/codex-save-counter.json` and reset after each prompt status line
is produced.

## CLI Baseline

The skills assume Cognee is available through the repository environment:

```bash
uv run cognee-cli --help
uv run cognee-cli remember "Cognee turns documents into AI memory." -d notes
uv run cognee-cli recall "What does Cognee do?" -d notes
uv run cognee-cli -ui
```

## Automatic Session Capture

The hook is fail-open and uses only Python's standard library. It redacts
secret-like keys and token-shaped values, truncates large payloads, and exits
successfully even when Cognee is unavailable so Codex work is not blocked.
Hook commands resolve through Codex's `PLUGIN_ROOT`, so they work from any
session working directory.
