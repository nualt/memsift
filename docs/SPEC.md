# memsift specification

What memsift does and how, precise enough to implement and test.
The reasoning behind each choice lives in [ARCHITECTURE.md](ARCHITECTURE.md).

Parts:

1. Layout
2. Files on disk
3. Hooks
4. Jev
5. Compactor
6. Failures
7. Configuration and tests

## 1. Layout

### Origin

memsift is a fork of the Claude Code and Codex integrations from
[cognee-integrations](https://github.com/topoteretes/cognee-integrations)
by Topoteretes UG, the makers of Cognee (Apache-2.0). In this document,
"upstream" means that project. memsift keeps upstream's `LICENSE` unchanged
and adds a `NOTICE` file that names the original project and its copyright
holder, as Apache-2.0 asks of derivative works.

### One plugin root, two hosts

Claude Code and Codex each copy the plugin root into their own cache, so
everything a hook runs must live under that root. The repo root is the plugin
root for both hosts: one copy of the code, two small manifests.

```
memsift/
├── .claude-plugin/
│   ├── plugin.json            Claude Code manifest, "hooks": "./hooks/claude-code.json"
│   └── marketplace.json       plugin source "./"
├── .codex-plugin/
│   └── plugin.json            Codex manifest, "hooks": "./hooks/codex.json"
├── .agents/plugins/
│   └── marketplace.json       Codex marketplace, plugin path "./"
├── hooks/
│   ├── claude-code.json       ${CLAUDE_PLUGIN_ROOT}/memsift/run.py <event>
│   └── codex.json             ${PLUGIN_ROOT}/memsift/run.py <event>
├── commands/
│   ├── memory-resume.md       /memory-resume, after an outage
│   └── memory-doctor.md       /memory-doctor, checks the setup
├── memsift/                   the code, Python standard library only
│   ├── run.py                 entry point: reads the hook JSON, calls the core,
│   │                          prints the host's answer, never crashes the host
│   ├── host.py                host differences: input fields, output shape
│   ├── core.py                start / prompt / tool / stop / end
│   ├── store.py               files on disk (part 2), append-only, locks
│   ├── jev.py                 Jev requests and questions (part 4)
│   ├── compactor.py           Gemma compaction (part 5)
│   ├── cognee.py              Cognee HTTP: recall, add, datasets, items
│   ├── breaker.py             3 failures in a row → pause (part 6)
│   ├── doctor.py              config, keys, Cognee and Jev reachable (part 6)
│   └── vendor/                kept from upstream, see below
├── tests/
├── docs/
│   ├── SPEC.md
│   └── ARCHITECTURE.md
├── LICENSE                    upstream's, unchanged
├── NOTICE                     origin and copyright of upstream
└── README.md
```

`host.py` holds every difference between hosts. Claude Code and Codex hooks
share the same events and almost the same JSON, so one module with a few
branches is enough. A third host (Cursor, Gemini CLI) adds branches there and a
new `hooks/<host>.json`.

To verify while implementing: Codex accepts a marketplace plugin path of `"./"`
(upstream used `"./plugins/cognee"`), and how a Codex plugin exposes
`/memory-resume` and `/memory-doctor` (Claude Code uses `commands/`).

### What we keep from upstream

Upstream is 22 000 lines. memsift keeps eight small modules that stand alone
(no import of the rest of the plugin), moved with `git mv` so their history
follows. Upstream files carry no per-file header, so each kept file gets one:

```
# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
```

| Upstream file | Lines | Becomes | Why keep it |
|---|---|---|---|
| `_recall_http.py` | 376 | `vendor/recall_http.py` | Cognee recall over HTTP, timeouts, error classes |
| `_remember_http.py` | 375 | `vendor/remember_http.py` | Cognee add, multipart upload, named items |
| `_recall_text.py` | 372 | `vendor/recall_text.py` | strips the prompt wrapper Cognee returns around recalled passages |
| `_capture_policy.py` | 198 | `vendor/redact.py` | redacts secrets before anything leaves the machine |
| `_env_file.py` | 284 | `vendor/env_file.py` | reads `~/.cognee/.env` |
| `_file_lock.py` | 53 | `vendor/file_lock.py` | cross-process lock for compaction |
| `_logfiles.py` | 203 | `vendor/logfiles.py` | bounded append-only logs (debug log, recall audit) |
| `_dataset_access.py` | 41 | `vendor/dataset_access.py` | dataset id / name fields, used by the two HTTP modules |

`hook_runner.py` is not kept: `run.py` does the same job (catch everything,
log it, exit 0) in a few lines.

### What we drop

Everything else, about 20 000 lines:

| Dropped | Lines | Replaced by |
|---|---|---|
| `session-start.py`, `_plugin_common.py` | 8 300 | `core.py` start, `store.py` |
| local mode (a Cognee server built in a venv on the machine) | inside the above | nothing: memsift talks to a running Cognee server |
| `session-context-lookup.py` (recall on every prompt) | 930 | `core.py` prompt, `jev.py` |
| `store-to-session.py`, `store-user-prompt.py`, `sync-session-to-graph.py`, `pre-compact.py` | 1 500 | `core.py` tool / stop / end, `compactor.py` |
| `_cognee_client.py` (circuit breaker) | 240 | `breaker.py`, one breaker for Jev, compactor and Cognee |
| observers, idle and exit watchers, `_proc.py` | 2 500 | nothing: the hooks are enough |
| status line, credits refresh | 960 | nothing |
| code graph, repo indexing, file context on `Read` | 1 700 | nothing |
| recap, switch-dataset, list-datasets, config, event names | 2 850 | `cognee.py`, environment variables (part 7) |
| `doctor.py` (checks upstream's setup: venv, local server, cloud credits) | 450 | our own `doctor.py`, much smaller, same idea |
| shell wrappers (`cognee-*.sh`, `cognee-plugin`) | 800 | nothing |
| 9 skills, 1 agent | | nothing: explicit search and remember stay with Cognee's own MCP server |
| `integrations/codex/` scripts | 22 000 | the same `memsift/` code as Claude Code |
| upstream `README.md`, `CHANGELOG.md`, `EVENTS.md` | | our `README.md`, `docs/` |

### Dependencies

Python 3.9 or later, standard library only. No virtualenv, nothing to
install: a hook runs `python3 run.py <event>` with the system interpreter.
