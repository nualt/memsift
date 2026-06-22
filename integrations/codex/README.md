# Cognee Codex Plugin

Adds persistent Cognee memory to Codex CLI.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

Install via the Codex marketplace:

```toml
# ~/.codex/config.toml
[features]
hooks = true
```

```bash
codex plugin marketplace add topoteretes/cognee-integrations --ref main
codex plugin add cognee@cognee
```

Then set environment variables for your runtime mode.

**Cognee Cloud or a remote server** — set both:

```bash
export COGNEE_BASE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

**Local mode** (default when `COGNEE_BASE_URL` is not set) — the plugin bootstraps a local Cognee API at `http://localhost:8011`. Only `LLM_API_KEY` is required; `COGNEE_API_KEY` is auto-minted if absent:

```bash
export LLM_API_KEY="sk-..."
```

You can also set config in `~/.cognee-plugin/config.json`:

```json
{
  "base_url": "https://your-instance.cognee.ai",
  "dataset": "cognee_sessions"
}
```

On startup you should see a "Cognee Memory Connected" system message.

## Auth

The integration uses a **single auth principal** — one API key, one user. No per-agent credentials.

Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Mode selection rules

At startup (`SessionStart`):
- `COGNEE_BASE_URL` set → `managed_endpoint`
- otherwise → `integration_local` (local API bootstrap)

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `api_key.json`), not only config intent
- `http` mode skips local SDK initialization
- `local_sdk` mode runs `ensure_cognee_ready(...)`

## Sessions

Each terminal launch maintains a small map file:

```
~/.cognee-plugin/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "..." }
```

- **`session_id`** — which Cognee session this terminal writes to and recalls from. Fixed at launch.
- **`conn_uuid`** — per-launch liveness handle used for agent registration and server shutdown counting.

By default a new `session_id` is generated each launch. Set `COGNEE_SESSION_ID` to resume a specific session:

```bash
export COGNEE_SESSION_ID="my-project"
codex
```

Two terminals can deliberately share a session by setting the same `COGNEE_SESSION_ID`.

## Dataset

All session data is written to a single dataset. By default both the Claude Code and Codex plugins write to the same dataset (`cognee_sessions`), so memory is shared across both integrations.

Set a custom dataset at launch:

```bash
export COGNEE_PLUGIN_DATASET="my-project-memory"
codex
```

Or persist it in `~/.cognee-plugin/config.json`:

```json
{ "dataset": "my-project-memory" }
```

The dataset is fixed for the lifetime of a launch — it cannot be changed mid-session.

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

## Status visibility

Cognee dataset status is shown as:

`cognee: <dataset-name>`

It is rendered by the Cognee statusline renderer using the same resolution order as the hooks:
1. `COGNEE_PLUGIN_DATASET` env var
2. `~/.cognee-plugin/config.json` → `dataset` key
3. Default: `cognee_sessions`

## Logs and state

Plugin state and logs are written under:

```bash
~/.cognee-plugin/codex/
```

Useful logs:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
tail -f ~/.cognee-plugin/codex/recall-audit.log
tail -f ~/.cognee-plugin/codex/exit-watcher.log
tail -f ~/.cognee-plugin/codex/watcher.log
```

## Update or remove

Reinstall plugin after marketplace/plugin changes:

```bash
codex plugin remove cognee@cognee
codex plugin add cognee@cognee
```

Remove plugin and marketplace:

```bash
codex plugin remove cognee@cognee
codex plugin marketplace remove cognee
```

## Configuration reference

Config precedence:
1. env vars
2. `~/.cognee-plugin/config.json`
3. defaults

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `cognee_sessions` | Dataset name |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `codex` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |

## Troubleshooting

**SessionStart hook invalid JSON output**
- Check `hook.log` and confirm the installed plugin version matches the expected hook contract.

**No new behavior after local edits**
- Codex may still be running a cached Git marketplace copy. Confirm installed marketplace/plugin source, then reinstall from the intended source.

**Startup / local endpoint issues**

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
curl -sS http://localhost:8011/health
```

**Unauthorized / key errors**
- Check `~/.cognee-plugin/api_key.json`. Delete it to force a re-mint.
- Relevant logs: `api_key_cached`, `api_key_minted`, `agent_register_result`.

**Missing session key at startup**
- If the payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

**Final sync diagnostics**
- Check `~/.cognee-plugin/codex/hook.log` and `~/.cognee-plugin/codex/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.
