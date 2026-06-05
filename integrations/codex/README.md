# Cognee Codex Plugin

This directory is a local Codex plugin marketplace for Cognee. The plugin uses
the installed `cognee` Python package, captures Codex session events into
Cognee session memory, recalls relevant memory on each prompt, and syncs
session memory into Cognee's graph during compaction, supported session-end
events, idle periods, or after the owning Codex process exits.

The setup below is for **Codex CLI**: the terminal-based `codex` command.
Start `codex` from the same shell where the Cognee virtual environment and LLM
API key are active.

## Install

Create or activate the Python environment where `cognee` is installed:

```bash
cd /path/to/your/project
python3 -m venv .venv
source .venv/bin/activate
python -m pip install cognee
```

Enable Codex hooks in `~/.codex/config.toml`:

```toml
[features]
hooks = true
plugin_hooks = true
```

Add this local marketplace and install the plugin:

```bash
cd /path/to/cognee-integrations/integrations/codex
codex plugin marketplace add .
codex plugin add cognee@cognee-local
```

Start Codex from the same environment where `cognee` is installed:

```bash
cd /path/to/your/project
source .venv/bin/activate
export LLM_API_KEY="your-key"
codex
```

## Configuration

The integration now runs in API mode and chooses endpoint mode at SessionStart.

Mode selection in `backend=auto`:

- If both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` are set:
  - Uses that endpoint (`managed_endpoint`).
  - The integration does not auto-start a local server.
- If one or both are missing:
  - Uses integration-managed local endpoint (`integration_local`).
  - Starts/uses local Cognee API on `http://localhost:8011`.

You can still force HTTP-style behavior explicitly:

```bash
export COGNEE_CODEX_BACKEND=http
```

`COGNEE_CODEX_BACKEND` is still supported for backward compatibility, but in
current integration logic the effective runtime endpoint selection is primarily
driven by whether both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` are present.
Treat `COGNEE_CODEX_BACKEND` as a legacy/override flag.

### Important Environment Variables

```bash
# Endpoint selection
export COGNEE_SERVICE_URL="http://localhost:8011"   # or cloud URL
export COGNEE_API_KEY="your-owner-or-tenant-key"
export COGNEE_CODEX_BACKEND="auto"                  # default

# Agent identity
export COGNEE_AGENT_NAME="codex-agent"

# Bootstrap owner credentials (used only when owner API key is missing and
# integration needs to create one to create/recreate the named agent)
export COGNEE_USER_EMAIL="default_user@example.com"
export COGNEE_USER_PASSWORD="default_password"
```

### Agent Lifecycle At Session Start

In API mode, `SessionStart` now:

1. Resolves Codex session key from SessionStart payload `session_id`.
2. Picks endpoint mode (`managed_endpoint` or `integration_local`).
3. Resolves/creates named agent credentials.
4. Registers this session via `/api/v1/agents/register`.
5. Stores per-agent API keys in:
   - `~/.cognee-plugin/codex/agent_keys.json`

Key cache entries are keyed by:

- `service_url::agent_name`

## Update Or Remove

After editing this plugin, reinstall it so Codex refreshes the cached copy:

```bash
cd /path/to/cognee-integrations/integrations/codex
codex plugin remove cognee@cognee-local
codex plugin add cognee@cognee-local
```

To remove the marketplace too:

```bash
codex plugin remove cognee@cognee-local
codex plugin marketplace remove cognee-local
```

## Logs And State

Plugin state and hook logs are written under:

```bash
~/.cognee-plugin/codex/
```

Useful files:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
tail -f ~/.cognee-plugin/codex/recall-audit.log
tail -f ~/.cognee-plugin/codex/exit-watcher.log
tail -f ~/.cognee-plugin/codex/watcher.log
```

Important state files:

```bash
~/.cognee-plugin/codex/agent_keys.json
~/.cognee-plugin/codex/exit-watchers/*.pid
```

Note: runtime state is resolved from Cognee HTTP endpoints (`/api/v1/users/me`
and `/api/v1/agents/connections/me`), not from `resolved.json` files.

## What The Hooks Do

- `SessionStart`: resolves session key, selects endpoint mode, ensures/creates agent credentials, registers active connection, starts idle and exit watchers.
- `UserPromptSubmit`: recalls session, trace, graph context, and graph memory.
- `PostToolUse`: stores tool calls as Cognee trace entries.
- `Stop`: stores the assistant response paired with the pending user prompt.
- `PreCompact`: emits a compact session/trace memory anchor and starts graph sync.
- `SessionEnd`: starts graph sync when the Codex client dispatches this hook.
- Detached sync on final exit can unregister the agent connection.

## Troubleshooting

### `401 Unauthorized` After Agent Setup

Common causes:

- Stale cached API key in `~/.cognee-plugin/codex/agent_keys.json`.
- Reused `COGNEE_AGENT_NAME` against a different endpoint with mismatched key.

Suggested recovery:

```bash
rm ~/.cognee-plugin/codex/agent_keys.json
```

Then restart Codex so SessionStart recreates and registers the agent.

### SessionStart Timeout While Starting Local API

If hooks time out on startup, verify local API health and logs:

```bash
curl -sS http://localhost:8011/health
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
```
