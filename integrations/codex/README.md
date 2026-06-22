# Cognee Codex Plugin

Adds persistent Cognee memory to Codex CLI.

The integration:
- captures prompts, tool traces, and assistant responses into Cognee session memory
- recalls relevant memory on each prompt
- syncs session memory into graph memory during compaction, idle/final exit paths, and supported session-end flows

## Install From GitHub Marketplace

1. Ensure Codex hooks are enabled in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

2. Add the Cognee marketplace from GitHub and install the plugin:

```bash
codex plugin marketplace add topoteretes/cognee-integrations --ref main
codex plugin add cognee@cognee
```

3. Optional: verify install:

```bash
codex plugin list --marketplace cognee --available --json
```

## Required Environment (Minimal)

For local/integration-managed mode, only this is required:

```bash
export LLM_API_KEY="your-llm-api-key"
```

Then start Codex from the same shell:

```bash
codex
```

## Session Model

- Each new Codex terminal launch starts a new Cognee session by default.
- Codex host `session_id` and Cognee `session_id` are different:
  - Codex host session id is a local correlation key.
  - Cognee session id is the memory scope (what recall/save uses).
- Session switching is per terminal. Other terminals keep their own active session unless you intentionally choose the same one.

## Choose Session At Startup (Optional Override)

If you want a specific Cognee session at launch time:

```bash
export COGNEE_SESSION_ID="my-session-id"
codex
```

This is an optional override. If not set, the plugin mints a fresh Cognee session for the new Codex launch.

## Switch Session During Use

You can switch without restarting Codex.

Ask Codex in natural language, for example:
- `Use cognee-configure-session and show my available sessions.`
- `Switch Cognee session to <session-id>.`
- `Create a new Cognee session named <name> and switch to it.`

Behavior:
- Existing id: resumes that session.
- New id/name: creates a new session and switches to it.
- From the next message onward, recall/save use the new session.

If your Codex build exposes skills in `/skills`, you can also invoke `cognee-configure-session` from there, but do not rely on `/skills` availability.

## Status Visibility

Cognee session status is shown in plugin messages as:

`cognee: <current_session> (+N more)`

It appears on:
- session startup message
- prompt memory recall header
- session switch result

Codex footer/status line can show native Codex items (including host session id), but Cognee custom status is not a persistent custom footer segment.

## Runtime Modes

For cloud/managed endpoint mode, set both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY`.

Mode selection at SessionStart:
- If both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` are set:
  - plugin targets that endpoint
- If either value is missing:
  - plugin uses integration-managed local endpoint (`http://localhost:8011`)
  - local bootstrap/install path is used as needed

## Hooks

- `SessionStart`: resolve session mapping, initialize runtime, register/start watchers
- `UserPromptSubmit`: recall context and stage user prompt
- `PostToolUse`: store tool trace
- `Stop`: store assistant response
- `PreCompact`: build memory anchor and begin sync path
- `SessionEnd`: trigger session-end sync path

## Logs And State

Plugin state/logs are written under:

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

## Update Or Remove

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

## Troubleshooting

### SessionStart hook invalid JSON output

If Codex reports SessionStart hook JSON output errors, the SessionStart hook output schema is invalid for the current Codex hook contract. Check `hook.log` and confirm the installed plugin version.

### No new behavior after local edits

Codex may still be running a cached Git marketplace copy, not your local plugin checkout. Confirm installed marketplace/plugin source, then reinstall from the intended source.

### Startup/local endpoint issues

Check:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
curl -sS http://localhost:8011/health
```
