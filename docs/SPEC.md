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
│   ├── memsift-resume.md       /memsift-resume, after an outage
│   └── memsift-doctor.md       /memsift-doctor, checks the setup
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

## 2. Files on disk

### Overview

```
~/.memsift/
├── datasets.md             one line per Cognee dataset
├── state.json              per machine: consecutive failures and pause, for Jev, compactor, Cognee (part 6)
├── debug.log               Jev decisions in plain text, if MEMSIFT_DEBUG=1
├── sessions/<session>.json one per running session, deleted at session end
└── repos/<repo-key>/
    ├── digest.md           compacted project memory, written by the compactor  ≤ 3 000 tokens
    ├── draft.md            entries added since the last compaction
    ├── pending.jsonl       turns noted while Jev was unreachable
    └── cognee-index.json   what Cognee holds, rebuilt each session

<repo root>/jev.md          project card, owned by the user
```

**Working notes** means `digest.md` followed by `draft.md`. Jev always reads
both; nothing is copied from one to the other. Compaction starts when digest + draft reach
8 000 tokens; it rewrites `digest.md` and empties `draft.md` (part 5).

### Common rules

- **Encoding.** UTF-8, `\n` line endings.
- **Tokens** are estimated as `ceil(characters / ratio)`. Jev's tokenizer is not
  public and there is no endpoint to count in advance, but every Jev response
  reports the real input count (`usage.input_tokens`). After each call the ratio
  is updated (moving average of real ratios), kept in `state.json` and shared by
  every session. It starts at 4. The debug log records estimate and real count.
  Jev's limits: 32 000 tokens for `state` plus the longest question, 64 000 per
  request (docs.typesafe.ai/models).
- **Secrets.** Every text is passed through `vendor/redact.py` before it is
  written and before it is sent (Jev, compactor, Cognee), so no file under
  `~/.memsift/` and no request ever holds a secret. It masks API keys, tokens,
  private keys and values of credential-like variables (`PASSWORD=`, `API_KEY=`)
  as `[redacted:<kind>]`.
- **Appends** (`draft.md`, `pending.jsonl`, `debug.log`) are a
  single `write` of whole lines on a file opened in append mode. Concurrent
  sessions may append to the same file at any time.
- **Rewrites** (`digest.md`, `draft.md` when it
  is emptied, `state.json`, `cognee-index.json`, session files) happen under
  `vendor/file_lock.py` on `<file>.lock`: write `<file>.tmp`, then rename over
  `<file>`. A reader never sees a half-written file.
- **Missing files** are treated as empty. memsift creates directories on demand.

### Repo key

A repo is the git top-level of the session's working directory, or the working
directory itself outside git. Its key is the directory name plus 6 hex
characters of the SHA-256 of its absolute path, so two repos with the same
name never share notes:

```
~/code/acme-shop       →  acme-shop-3f2a91
~/clients/acme-shop    →  acme-shop-8c04d2
```

### Draft entries

Jev cannot write text: it only returns probabilities. A `draft.md` entry is
therefore the raw turn, with labels added by code. Only the compactor writes
sentences (part 5).

```
- [d1] 2026-10-14 decision
  User: no, the header must reuse the footer grid
  Agent: ok, the header now uses the footer's 12-column grid
  Actions: Edit src/header.tsx · Bash: pnpm build (ok)
```

| Field | Source |
|---|---|
| `id` | `d` and a number unique in the file, given by code |
| date | `YYYY-MM-DD`, the day of the turn |
| nature | Jev's answer: `working_rule` (for this project only), `decision`, `project_fact` or `lesson` (part 4) |
| `User:` | the prompt, as received by the prompt hook |
| `Agent:` | the agent's answer text, as received by the stop hook; tool calls and their output are never included |
| `Actions:` | the action note of the turn (part 3), omitted when empty |

The prompt and the answer arrive in separate hooks, so they are always labelled
correctly. Before writing, each is cut if too long, keeping the start and the
end with `[…]` in between:

| Part | Limit | Measured on 5 163 real turns |
|---|---|---|
| `User:` | 1 500 tokens | 97 % of prompts are shorter; longer ones are mostly pasted logs or files. About 7 minutes of dictation fit. |
| `Agent:` | 1 000 tokens | 99.9 % of answers are shorter (longest seen: 2 020) |

Jev always reads the full turn to decide; the limits only apply to what is
written. Continuation lines are indented by two spaces; an entry ends at the
next line starting with `- [`. `draft.md` starts with `# Draft: <repo-key>`.

### Corrections

A correction is a turn where the user corrects the agent. Its entry is marked
pending:

```
- [d4] 2026-10-14 decision · correction · pending
  User: no, the product grid has 12 columns, not 10
  Agent: ok, switching the product grid to 12 columns
  Actions: Edit src/product-grid.tsx
```

While it is pending, each following stop hook adds one question to its Jev
request: is this correction confirmed by what happened since (part 4)? Jev reads
the correction, the recent turns and the action notes of the last 5 turns. When
the answer passes the threshold, the hook appends one line at the end of the
file; the entry itself is never edited:

```
- [d4] confirmed
```

At compaction, the compactor applies confirmed corrections, finding by itself
the older statement they replace, and carries pending ones over to the new
`draft.md`. A change of mind is a new correction; the most recent wins.

### digest.md

Per repo. Written only by the compactor, never appended to. Entries of nature
`decision` and `project_fact`, work in progress first, then most recent first.
`lesson` entries never go here: they live in Cognee only.

### draft.md

Per repo. Entries appended by Jev at the end of each turn, all four natures,
plus correction status lines. Emptied by compaction, except the lines appended
while the compaction was running and corrections still pending.

### datasets.md

What each Cognee dataset is for, in the user's words. Jev reads it to choose
where to search. One line per dataset:

```
# Datasets
- memsift: memsift's own memory, written at compaction.
- playbooks: how we run recurring work: deploys, onboarding, client handover.
- projects: one file per client project: stack, contacts, decisions.
- lessons: technical problems we hit and what fixed them.
- articles: IGNORED
- posts: IGNORED
```

`IGNORED` datasets are never searched and are left out of `cognee-index.json`.
At session start, a dataset on the server that has no line here raises a flag:
the agent is asked to propose a description to the user, and writes the line
once the user approves. memsift never writes this file itself.

### cognee-index.json

A cache of item names, so Jev can tell whether a topic is in Cognee without
asking it. Rebuilt in the background at each session start from
`GET /api/v1/datasets` and `GET /api/v1/datasets/{id}/data`; never edited.

```json
{
  "fetched_at": "2026-10-14T07:02:11Z",
  "datasets": [
    {
      "name": "memsift",
      "id": "6f1c…",
      "items": ["acme-shop 2026-10-13 header grid and footer alignment",
                "acme-shop 2026-10-12 pnpm build scripts and sharp"]
    }
  ]
}
```

For the `memsift` dataset, only this repo's packets are listed (their names
start with the repo name), newest first. Every other dataset not marked
`IGNORED` is listed in full. The file is cut, oldest `memsift` packets first, so
it stays under 1 000 tokens. Packets sent by the compactor are named
`<repo name> <date> <topics>` so that Jev can read them (part 5).

memsift writes only to its own dataset, `memsift` by default
(`MEMSIFT_DATASET`), never to datasets other tools write to.

### pending.jsonl

Per repo. One JSON object per turn while Jev is paused (part 6), sorted by Jev
on `/memsift-resume`, then the file is deleted.

```json
{"ts": "2026-10-14T07:31:02Z", "session": "a1b2…", "prompt": "…", "answer": "…", "actions": ["Edit src/header.tsx", "git commit"]}
```

`prompt` and `answer` are cut with the same limits as draft entries
(1 500 and 1 000 tokens, start and end kept). On resume, Jev sorts the cut
turns, since the full ones were not kept.

### sessions/\<session\>.json

Short-lived state of one running session, rewritten by its own hooks only.
Each hook is a separate process, so this file is how one hook hands information
to the next. Deleted at session end; a session that crashed leaves its file
behind, so session start removes session files older than 7 days.

```json
{
  "repo_key": "acme-shop-3f2a91",
  "host": "claude-code",
  "source": "startup",
  "started_at": "2026-10-14T07:02:10Z",
  "actions": [
    {"turn": 2, "items": ["Edit src/product-grid.tsx: grid-cols-10 → grid-cols-12"]},
    {"turn": 3, "items": ["Bash: git commit -m \"product grid in 12 columns\" (ok)"]}
  ],
  "flags": ["jev_md_missing"],
  "outage_notice_sent": false
}
```

| Field | Meaning |
|---|---|
| `source` | how the session started: `startup`, `resume`, `clear`, `compact` |
| `actions` | the action notes of the last 5 turns (part 3); an `Edit` keeps a diff of about 200 characters, a `Write` only its file name |
| `flags` | messages for the agent at the next prompt: `jev_md_missing`, `jev_md_outdated`, `dataset_undescribed:<name>` |
| `outage_notice_sent` | the one-line outage notice was already given (part 6) |

### jev.md

The project card, at the repo root, next to `CLAUDE.md`. Jev reads it on every
call; it is never pasted into the agent's context. It describes what weighs in
this project, not how to work in it. At most 3 000 tokens, in practice a dozen
lines.

```
# jev.md
project: acme-shop, an e-commerce store
stakes: payments, stock, orders, customer data
        → any decision touching these is essential
phase: setup, before launch
documented in: README.md, docs/
do not keep: commit status, one-off visual tweaks
```

| Line | Purpose |
|---|---|
| `project` | what the repo is, in one line |
| `stakes` | where the important decisions are; the line that does most of the work |
| `phase` | where the project stands; changes what counts as news |
| `documented in` | files that already hold knowledge: counts as "already known" |
| `do not keep` | what this project never needs remembered |

memsift never writes `jev.md`. When it is missing or looks outdated, a flag asks
the agent to propose a version, and the agent writes it once the user approves.
A repo that must not show it adds it to its `.gitignore`.
