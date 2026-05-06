# Cognee Plugin Marketplace for Codex

This integration packages Cognee skills and hooks for Codex as a local Codex
plugin marketplace. It exposes CLI-first workflows for Cognee setup, memory,
codebase ingestion, local UI launch, and automatic Codex session capture.

## Contents

- `.agents/plugins/marketplace.json` - local Codex marketplace definition.
- `plugins/cognee/.codex-plugin/plugin.json` - Cognee Codex plugin manifest.
- `plugins/cognee/hooks.json` - Codex lifecycle hooks for Cognee session
  capture and recall injection.
- `plugins/cognee/skills/` - reusable Codex skills for Cognee CLI workflows.
- `plugins/cognee/scripts/cognee-codex-hook.py` - hook handler that posts
  session entries to Cognee and recalls prompt context from the backend.
- `plugins/cognee/scripts/cognee-cli.sh` - helper that runs `uv run cognee-cli`
  from a Cognee repository root.

## Local Install

From this directory:

```bash
codex plugin marketplace add .
```

Restart Codex, open the plugin directory, select `Cognee Local Plugins`, and
install `Cognee`.

Automatic capture also requires Codex hooks to be enabled:

```toml
[features]
codex_hooks = true
```

The hook reads Cognee connection details from `COGNEE_SERVICE_URL` /
`COGNEE_API_KEY` or `~/.cognee/cloud_credentials.json`. If no URL is
configured, it uses `http://localhost:8000`. It writes to the `codex_sessions`
dataset unless `COGNEE_CODEX_DATASET` is set. Prompt recall searches
`session,trace,graph_context,graph` by default; override with
`COGNEE_CODEX_RECALL_SCOPE`.

## CLI Baseline

The skills assume Cognee is available through the repository environment:

```bash
uv run cognee-cli --help
uv run cognee-cli remember "Cognee turns documents into AI memory." -d notes
uv run cognee-cli recall "What does Cognee do?" -d notes
uv run cognee-cli -ui
```
