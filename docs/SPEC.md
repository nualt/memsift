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
│   └── plugin.json            Codex manifest, "hooks": "./hooks/codex.json",
│                              "skills": "./skills/"
├── .agents/
│   └── plugins/
│       └── marketplace.json   Codex marketplace, plugin path "./"
├── hooks/
│   ├── claude-code.json       ${CLAUDE_PLUGIN_ROOT}/memsift/run.py <event>
│   └── codex.json             ${PLUGIN_ROOT}/memsift/run.py <event>
├── commands/
│   ├── memsift-resume.md       Claude Code: /memsift-resume, after an outage
│   └── memsift-doctor.md       Claude Code: /memsift-doctor, checks the setup
├── skills/
│   ├── memsift-resume/
│   │   └── SKILL.md           Codex: $memsift-resume, after an outage
│   └── memsift-doctor/
│       └── SKILL.md           Codex: $memsift-doctor, checks the setup
├── memsift/                   the code, Python standard library only
│   ├── run.py                 entry point: reads the hook JSON, calls the core,
│   │                          prints the host's answer, never crashes the host
│   ├── host.py                host differences: input fields, output shape
│   ├── core.py                start / prompt / tool / stop / end
│   ├── store.py               files on disk (part 2), append-only, locks
│   ├── jev.py                 Jev requests and questions (part 4)
│   ├── compactor.py           compaction (part 5)
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

Claude Code exposes the two recovery commands through `commands/`. Codex
exposes them as skills through `skills/`: invoke `$memsift-resume` or
`$memsift-doctor`, or ask the agent to run that skill. Both reach the same
`memsift/run.py resume` and `doctor` entry points. A Codex skill resolves the
plugin root two directories above its containing directory and shows the
runner's output to the user.

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
| `_logfiles.py` | 203 | `vendor/logfiles.py` | bounded append-only logs (`debug.log`) |
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
├── debug.log               every error, always; every decision if MEMSIFT_DEBUG=1 (part 7)
├── sessions/<session>.json one per running session, deleted at session end
└── repos/<repo-key>/
    ├── digest.md           compacted project memory, written by the compactor  ≤ 3 000 tokens
    ├── draft.md            entries added since the last compaction
    ├── pending.jsonl       turns noted while Jev was unreachable
    ├── outbox/             packets waiting for Cognee (part 6)
    ├── compaction.lock     held while a compaction runs (part 5)
    ├── replay.lock         held while pending turns are sorted (part 6)
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
  every session. It starts at 3, the ratio measured on real requests mixing
  French text and a JSON state (3.0 to 3.2). The debug log records estimate and
  real count.
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
`draft.md`. A confirmation appended while a compaction runs is carried over with
its correction, for the next compaction. A change of mind is a new correction;
the most recent wins.

### digest.md

Per repo. Written only by the compactor, never appended to. Holds what entries
of nature `working_rule`, `decision` and `project_fact` say, work in progress
first, then most recent first. `lesson` entries never go here: they live in
Cognee only. Format in part 5.

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
`IGNORED` is listed in full when it fits. The file stays under 1 000 tokens: it
is cut, oldest `memsift` packets first, then the item lists of the other
datasets, each cut from its end to an equal share. Dataset names never are. A
cut is written to `debug.log`. Packets sent by the compactor are named
`<repo name> <date> <time> <topics>` so that Jev can read them (part 5).

memsift writes only to its own dataset, `memsift` by default
(`MEMSIFT_DATASET`), never to datasets other tools write to.

### pending.jsonl

Per repo. One JSON object per turn that Jev could not sort: while Jev is paused
(part 6), or when the stop hook's Jev call failed (timeout, error, rate limit).
Sorted by Jev on `/memsift-resume`, or by the next successful stop hook in that
repo, then the file is deleted. One sorter at a time per repo, under
`replay.lock`; a second one finding it taken does nothing.

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
  "repo_root": "/home/dev/code/acme-shop",
  "host": "claude-code",
  "source": "startup",
  "started_at": "2026-10-14T07:02:10Z",
  "turn": 3,
  "prompt": "commit it",
  "snapshot": "9b1e…",
  "actions": [
    {"turn": 2, "prompt": "the product grid has 12 columns, not 10",
     "items": ["Edit src/product-grid.tsx: grid-cols-10 → grid-cols-12"]},
    {"turn": 3, "prompt": "commit it",
     "items": ["Bash: git commit -m \"product grid in 12 columns\" (ok)"]}
  ],
  "flags": ["jev_md_missing"],
  "outage_notices": []
}
```

| Field | Meaning |
|---|---|
| `repo_root` | absolute path of the repo, found at session start; later hooks read it from here |
| `source` | how the session started: `startup`, `resume`, `clear`, `compact`; `fork` is stored as `resume` |
| `turn` | number of the current turn, increased by the prompt hook |
| `prompt` | the current prompt, kept by the prompt hook for the stop hook |
| `snapshot` | hash of the repo state (`HEAD` and the uncommitted diff), to tell whether a command changed something (part 3) |
| `actions` | the action notes of the last 5 turns (part 3); an `Edit` keeps a diff of about 200 characters, a `Write` only its file name. `prompt` holds the first 200 characters of that turn's prompt, to attach the note to the right exchange of the transcript |
| `flags` | messages for the agent at the next prompt: `jev_md_missing`, `jev_md_outdated`, `dataset_undescribed:<name>`, `decision_reason_missing:<id>` |
| `outage_notices` | services whose outage notice was already given in this session (part 6) |

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

## 3. Hooks

### Overview

```
SessionStart        read source, prepare           returns nothing      background: Cognee index
UserPromptSubmit    Jev decides what to paste      returns the context  the only hook that waits
PostToolUse         action note, local only        returns nothing
PostToolUseFailure  same, for a failed call        returns nothing      Claude Code only
Stop                Jev sorts the turn             returns nothing      background
SessionEnd          compact what is left           returns nothing      background
```

Both hosts call `memsift/run.py <event>` with the hook JSON on stdin. `run.py`
never fails the host: any error is written to `debug.log` with its traceback
(part 7) and the hook returns nothing.

**Background work.** The hook starts a detached copy of `run.py` with the same
input, plus everything it needs from the session file (prompt, action notes,
repo root), and returns at once. A detached process never recreates a session
file that session end has deleted: its flags are then dropped. An entry appended
after the session-end compaction stays in `draft.md` for the next one. The detached process outlives the hook, and the
session, so closing the agent never waits for memsift.

memsift registers no `PreCompact` hook: every turn worth keeping is already in
`draft.md` when the stop hook ends, so there is nothing to save before a
compaction.

### Session start

```
input    session_id, cwd, source, transcript_path
does     1. writes sessions/<session>.json: repo key, host, source
         2. removes session files older than 7 days
         3. jev.md missing at the repo root      → flag jev_md_missing
         4. in the background: rebuilds cognee-index.json, then compares the
            server's datasets with datasets.md
            dataset without a line               → flag dataset_undescribed:<name>
returns  nothing
```

Codex runs this hook only for the sources listed in its matcher, so
`hooks/codex.json` lists all of them: `startup|resume|clear|compact|fork`.

### Prompt

The only hook the user waits for.

```
input    session_id, prompt, transcript_path
does     1. turn + 1; keeps the prompt in the session file
         2. memsift paused (part 6)?  → outage notice once, then nothing
         3. Jev, one request, 2 s at most:
              ① is the agent missing information that already exists?
              ② for each notes entry: does it hold what is missing?
              ③ is it worth asking Cognee, and in which datasets?
         4. decides:
              P① < 0.7                            → nothing from memory
              P① ≥ 0.7, entries with P② ≥ 0.5     → those entries
              P① ≥ 0.7, no such entry, P③ ≥ 0.8   → Cognee
         5. Cognee: recall on the chosen datasets, 12 s at most,
            then Jev ④ sorts the passages, 2 s at most
returns  what was kept, then the pending flags, as additionalContext
```

The thresholds are starting points, set at calibration. The questions and
their criteria are in part 4.

**Waiting time.** Most prompts stop at Jev (under 2 s). When Cognee is asked,
the worst case is 2 + 12 + 2 = 16 s. Any step that times out ends the hook
with nothing from memory; unsorted Cognee passages are never pasted.

**What Jev reads.** Budget for one request, about 26 000 tokens, inside Jev's
32 000:

| Part | Tokens |
|---|---|
| prompt, questions, how the session started | 2 000 |
| `jev.md` | ≤ 3 000 |
| last compaction summary | ≤ 6 000 |
| recent exchanges | ~ 5 000 |
| working notes (`digest.md` + `draft.md`) | ≤ 8 000 |
| `cognee-index.json` | ≤ 1 000 |

How the session started, as told to Jev:

| `source` | What the agent has in mind |
|---|---|
| `startup`, `clear` | nothing: new session, the agent knows nothing of this repo |
| `compact` | only the compaction summary and what followed |
| `resume` | the whole previous conversation |

`CLAUDE.md` and `AGENTS.md` are not sent here: the agent already has them.

**Reading the conversation.** memsift reads the host's transcript
(`transcript_path`) from the end and never loads the whole file:

- **Claude Code.** Each compaction adds a `Conversation compacted` marker
  followed by the summary; nothing is removed. Only the last summary counts,
  since each one includes the previous.
- **Codex.** Each compaction adds a `compacted` record whose summary is
  encrypted. memsift sends instead the user messages that Codex keeps in clear
  in that record. Jev then knows what was discussed but not all the agent
  retained, so it may sometimes paste an entry the agent already has.
- **Recent exchanges.** The text of the prompts and answers since the last
  compaction, newest kept first. Tool calls and their output are dropped; each
  turn carries its action note instead, when the session file still has it.

```
transcript                                 sent to Jev
User: continue the footer                  User: continue the footer
Read footer.tsx → 240 lines                Agent: added the border on the left,
Edit footer.tsx → diff                       tests pass.
Bash pnpm test → 80 lines of output          Actions: Edit footer.tsx ×3 ·
Edit footer.tsx → diff                       pnpm test failed → ok · git commit
Bash pnpm test → 60 lines                  User: same for the header
Bash git commit → output
Agent: added the border on the left, …
User: same for the header
```

**Over budget.** Cut in this order: oldest recent exchanges, then the end of
the summary, then the oldest draft entries. The prompt, the questions and
`jev.md` are never cut. If Jev still refuses the request as too large, it is
retried once with half the recent exchanges; if that fails, nothing is pasted.
If the prompt, the questions and `jev.md` alone are too large, nothing is pasted
and the error is logged.

**What the agent receives.** One block per source, entries pasted as they are,
since Jev cannot write. When nothing was kept and no flag is pending, the hook
returns nothing at all.

```
memsift, from the working notes:
- [d3] 2026-10-12 decision
  User: no, the header must reuse the footer grid
  Agent: ok, the header now uses the footer's 12-column grid

memsift, from Cognee (dataset lessons):
- pnpm blocks dependency build scripts; allow them in package.json
  under pnpm.onlyBuiltDependencies.

memsift:
- This repo has no jev.md. Propose a first version to the user.
```

**Flags.** A flag is given once, at the next prompt, then removed from the
session file:

| Flag | Raised by | Message to the agent |
|---|---|---|
| `jev_md_missing` | session start | This repo has no jev.md. Propose a first version to the user. |
| `jev_md_outdated` | stop | jev.md looks outdated. Propose an update to the user. |
| `dataset_undescribed:<name>` | session start | Cognee dataset `<name>` has no line in datasets.md. Propose a description to the user, or IGNORED. |
| `decision_reason_missing:<id>` | stop | Entry `<id>` records a structuring decision without its reason. Ask the user why. |

### Tool use

Builds the turn's action note, in code. Nothing is sent anywhere.

| Call | Note |
|---|---|
| `Edit`, `MultiEdit`, `NotebookEdit` (Claude Code), `apply_patch` (Codex), on a file in the repo | `Edit <path>: <diff>`, diff cut to about 200 characters |
| `Write` on a file in the repo | `Write <path>`, file name only |
| any edit outside the repo (`/tmp`, scratchpad) | ignored |
| `Bash` that changed the repo | `Bash: <command> (ok)` |
| `Bash` that changed nothing | ignored |
| a command that failed, then succeeded in the same turn | `Bash: <command> failed → ok`, marked as a possible lesson, even if the repo did not change |
| a command that failed and was not run again in the turn | ignored |
| any other tool (reads, searches, web) | ignored |

**Did a command change the repo?** memsift compares a snapshot of the repo
taken after the call with the one kept in the session file. The snapshot is a
hash of `HEAD` and of the uncommitted diff (`git diff HEAD` plus the list of
untracked files), so it catches a commit, a new file, and a further change to
an already modified file alike. It costs three short `git` calls (`rev-parse
HEAD`, `diff HEAD`, `ls-files --others --exclude-standard`), a few tens of
milliseconds in all. It is refreshed after every edit too, so the next command is
compared with an up-to-date state. Outside a git repo, commands are not noted.

The action notes of the last 5 turns are kept in the session file.

### Stop

Runs in the background; the agent never waits for it.

```
input    session_id, last_assistant_message, transcript_path
does     1. memsift paused?  → appends the turn to pending.jsonl, stops
            pending.jsonl not empty → sorts it first (part 6)
         2. Jev, one request (part 4):
              nature: working_rule (this project) / decision / project_fact /
                      lesson / nothing
              is it a correction of the agent?
              each pending correction: confirmed?
              already known (working notes, CLAUDE.md, AGENTS.md)?
              does jev.md look outdated?
              a structuring decision given without its reason?
         3. worth keeping and not already known → entry appended to draft.md
            confirmed correction                → "- [dN] confirmed" appended
            jev.md outdated                     → flag jev_md_outdated
            structuring decision, no reason     → flag decision_reason_missing
         4. working notes ≥ 8 000 tokens → compaction (part 5)
returns  nothing
```

The prompt comes from the session file, the answer from
`last_assistant_message`, the action note from the session file. If the Jev
call fails (timeout, error, rate limit), the turn is appended to
`pending.jsonl` instead of being lost.

**What Jev reads.** About 21 000 tokens:

| Part | Tokens |
|---|---|
| prompt, answer, action notes of the last 5 turns | ~ 3 000 |
| questions and criteria | 2 000 |
| `jev.md` | ≤ 3 000 |
| `CLAUDE.md` and `AGENTS.md` at the repo root, those that exist | ≤ 3 000 together |
| working notes | ≤ 10 000 |
| recent exchanges, only when a correction is pending | ≤ 3 000 |

Over budget: the turn is cut with the start-and-end rule, the instruction files
keep their start, the notes lose their oldest entries.

`CLAUDE.md` and `AGENTS.md` are the same kind of file under two conventions
and are treated alike. Only the repo's own are read: general working rules are
never kept in the notes (part 2), so the user's global files add nothing here.

### Session end

Runs in the background, so the terminal closes at once.

```
input    session_id
does     1. draft.md not empty → compaction, even below 8 000 tokens:
            packets sent to Cognee, digest.md rewritten (part 5)
         2. deletes sessions/<session>.json
returns  nothing
```

A session that crashes runs no session end. Its entries stay in `draft.md`
and go out with the next compaction; its session file is removed after 7 days.

## 4. Jev

### Request

Jev is TypeSafe's classifier, reached through OpenRouter. It never writes text:
each question returns a probability, and code decides with thresholds.

```
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer $OPENROUTER_API_KEY
{"model": "typesafe/jev-1.13", "state": {…}, "questions": {"<id>": {…}, …}}
```

| Question type | Returns | Used for |
|---|---|---|
| `noul` | probability of yes, 0 to 1 | every yes/no question |
| `choice` | one option and the distribution | the nature of a turn |
| `score` | a position on ordered levels | the scope of a turn |

Rules that follow from how Jev works:

- **Questions are in English**, Jev's strongest language. The state stays in the
  user's language.
- **One judgment per question.** To pick items from a list (notes entries,
  datasets, passages), code asks one `noul` per item: Jev cannot write a list.
- **State is a JSON object with named fields**, and questions point at them by
  name (`notes.d3`). Each request sends only the fields its questions need: a
  large state full of unrelated text lowers Jev's accuracy.
- **No dates or numbers to compare.** Ordering and counting stay in code.
- The model is pinned to a version (`MEMSIFT_JEV_MODEL`), since the thresholds
  are tuned against it.

### Prompt request

Sent by the prompt hook. 2 s timeout.

```json
{
  "prompt": "…",
  "session_start": "new session: the agent knows nothing of this repo",
  "project_card": "<jev.md>",
  "compaction_summary": "…",
  "recent_exchanges": "…",
  "notes": {"g1": "…", "d3": "…"},
  "datasets": {"lessons": {"description": "<datasets.md line>", "items": ["…"]}}
}
```

`notes` holds every entry of `digest.md` and `draft.md` under its id.
`datasets` merges `datasets.md` and `cognee-index.json`, without `IGNORED`
datasets.

| Id | Type | Question |
|---|---|---|
| `missing` | noul | Does the agent lack information, needed to act on `prompt`, that already exists: said or decided before this conversation, and not in `compaction_summary` or `recent_exchanges`? |
| `entry_<id>` | noul | Does `notes.<id>` hold information the agent needs to act on `prompt` and does not have yet? |
| `cognee` | noul | Is it worth searching the long-term memory? |
| `dataset_<name>` | noul | Given its description and items, would `datasets.<name>` hold what the agent needs for `prompt`? |

Criteria for `missing`:

```
true    the prompt refers to work, a decision or a preference from before this
        conversation, which the agent cannot see
false   the information does not exist yet (it is being decided now), or the
        agent already has it, or the prompt needs no memory
```

Criteria for `cognee`:

```
true    the prompt refers to past work, a decision or a preference, absent from
        the conversation and the notes, and the agent would act differently
        knowing it
false   a general question, an answer found in the code or the docs, or memory
        would only be a bonus without changing what the agent does
```

Code:

```
missing < 0.7                                → nothing
entries with entry_<id> ≥ 0.5                → those entries, in notes order
no such entry, cognee ≥ 0.8                  → Cognee, in datasets with
                                               dataset_<name> ≥ 0.5, or the
                                               highest one if none passes
otherwise                                    → nothing
```

### Passage request

Sent after a Cognee recall. 2 s timeout. Its state holds only what the
judgment needs: `prompt`, `compaction_summary`, `recent_exchanges` and
`passages` (`p1` to `p5`). The notes are not sent: the prompt request already
found the answer was not there.

| Id | Type | Question |
|---|---|---|
| `useful_<n>` | noul | Would `passages.p<n>` change what the agent does for `prompt`? |
| `known_<n>` | noul | Is the information in `passages.p<n>` already in `compaction_summary` or `recent_exchanges`? |

Code: a passage is pasted when `useful ≥ 0.7` and `known < 0.5`.

### Stop request

Sent by the stop hook, in the background, and for each turn of
`pending.jsonl` on `/memsift-resume`.

```json
{
  "turn": {"prompt": "…", "answer": "…", "actions": ["…"]},
  "recent_actions": ["…"],
  "project_card": "<jev.md>",
  "instruction_files": "<CLAUDE.md and AGENTS.md at the repo root>",
  "notes": {"g1": "…", "d3": "…"},
  "pending_corrections": {"d4": "…"},
  "recent_exchanges": "…"
}
```

| Id | Type | Question |
|---|---|---|
| `nature` | choice | What kind of information does `turn` carry? |
| `scope` | score | How long does it stay true? |
| `correction` | noul | Does the user correct the agent in `turn`? |
| `confirmed_<id>` | noul | Do `turn`, `recent_actions` and `recent_exchanges` confirm `pending_corrections.<id>`: applied, and not taken back? |
| `known` | noul | Is it already in `notes` or `instruction_files`, or written this turn to a file listed under "documented in" in `project_card` (an edit in `turn.actions`)? |
| `card_outdated` | noul | Does `turn` make `project_card` outdated: a new stake, a change of phase, or a contradiction? |
| `reason_missing` | noul | Does `turn` take a structuring decision without giving its reason? |

`card_outdated` is asked only when `jev.md` exists, `confirmed_<id>` and
`recent_exchanges` only when corrections are pending. `recent_exchanges` is the
transcript tail read as in the prompt hook, so a correction taken back in a
text-only turn is not confirmed by an older edit.

`nature` options:

| Option | Criteria |
|---|---|
| `working_rule` | how the user wants the agent to work: form of answers, way of coding, steps, what to ask before acting |
| `decision` | a choice that steers what follows and cannot be read in the code or docs: the why, what was ruled out, where things stand |
| `project_fact` | lasting information about this project: an address, an account, a constraint, a business rule |
| `lesson` | a technical problem, and what worked or did not (a dead end saves trying it again) |
| `nothing` | status, reaction, small talk, typo, a one-off instruction, a micro-choice applied in the code |

`scope` levels: this turn · this task · this project · always.

`reason_missing` is about structuring decisions ("we go with Medusa"), not
micro-choices ("the button in blue").

Code:

```
nature nothing, or scope "this turn"           → dropped
working_rule with scope "always"               → dropped: general rules
                                                 belong in CLAUDE.md / AGENTS.md
known ≥ 0.5                                    → dropped
nature confidence < 0.6 (< 0.4 for a correction) → dropped
otherwise                                      → entry appended to draft.md,
                                                 "· correction · pending" when
                                                 correction ≥ 0.5
confirmed_<id> ≥ 0.7                           → "- [<id>] confirmed" appended
card_outdated ≥ 0.7                            → flag jev_md_outdated
kept decision with reason_missing ≥ 0.7        → flag decision_reason_missing:<id>
```

### Thresholds

Every number above is a starting point, kept in one place in `jev.py`, and set
at calibration: about 50 real prompts labelled by hand ("should memory have
pasted something, and what?"), run through Jev, and the thresholds that match
the labels best are kept. Calibration also checks:

- accuracy on prompts in the user's language, Jev being strongest in English;
- the prompt request with its full state against a minimal one (`jev.md` and
  notes only), keeping whichever errs less;
- the `nature` answer with its options in another order, since Jev can lean
  toward the first option.

## 5. Compactor

The compactor is the only part of memsift that writes sentences. It runs
Claude Haiku 5.5 by default, through OpenRouter with the same key as Jev. Any
OpenAI-compatible API can be used instead (`MEMSIFT_COMPACTOR_URL`,
`MEMSIFT_COMPACTOR_API_KEY`, `MEMSIFT_COMPACTOR_MODEL`).

### When

- At the end of a turn, when the working notes reach 8 000 tokens.
- At session end, when `draft.md` holds at least one entry.

Always in the background. One compaction at a time per repo, under a lock on
`repos/<repo-key>/compaction.lock`; a compaction that finds the lock taken
does nothing, since the running one will empty the draft.

### What it does

```
1. reads digest.md and draft.md, and remembers where draft.md ended;
   a draft holding only pending corrections → stops, nothing to compact
2. the compactor, one request → new digest + packet
3. code checks the answer (schema, ids, length, and a digest that comes back
   empty while the input had a digest line or a working_rule, decision or
   project_fact entry); invalid → one retry; invalid again → nothing is
   written, the notes stay as they were
   every invalid answer is written to debug.log with its reason (part 7)
4. writes the packet to the outbox
5. rewrites digest.md
6. rewrites draft.md: keeps the lines appended since step 1 and the
   corrections still pending
7. sends the outbox to Cognee, dataset memsift (part 6 if Cognee is down)
```

A crash after step 4 leaves a packet that is sent later and notes not yet
rewritten; the next compaction may then send overlapping content once.

Jev keeps reading the old notes until step 5; nothing is lost if a turn ends
during the compaction.

### Compactor request

```
POST $MEMSIFT_COMPACTOR_URL/chat/completions
{
  "model": "anthropic/claude-haiku-5.5",
  "temperature": 0.2,
  "messages": [{"role": "system", "content": "<instructions>"},
               {"role": "user", "content": "<digest.md>\n\n<draft.md>"}],
  "response_format": {"type": "json_schema", "json_schema": {"strict": true, …}}
}
```

The model may reason before it answers. Compaction runs in the background, so
the extra seconds cost nobody any waiting, and the extra tokens cost a fraction
of a cent.

Instructions:

- Keep the user's words. Invent nothing, add nothing.
- Merge duplicates. When two entries contradict each other, the most recent wins.
- Apply each confirmed correction: replace the statement it corrects.
  Leave pending corrections out; code carries them over.
- Lessons go to the packet only, never to the digest.
- Digest: work in progress first, then most recent first. Drop what is no
  longer true.
- Write in the language of the entries.

Answer schema:

```json
{
  "digest": {
    "in_progress":   [{"text": "…", "date": "YYYY-MM-DD"}],
    "decisions":     [{"text": "…", "date": "YYYY-MM-DD"}],
    "project_facts": [{"text": "…", "date": "YYYY-MM-DD"}],
    "working_rules": [{"text": "…", "date": "YYYY-MM-DD"}]
  },
  "packet": {
    "topics": "2 to 4 words",
    "text": "…"
  }
}
```

### digest.md

Code writes the file from the answer and numbers the lines; the compactor
never handles ids. Ids start again from `g1` at each compaction.

```
# Digest: acme-shop-3f2a91
## In progress
- [g1] header: reuses the footer grid, 12 columns (2026-10-12)
## Decisions
- [g2] payment through Stripe Elements; Stripe Checkout ruled out, not customisable enough (2026-10-08)
## Project facts
- [g3] sharp must be allowed in pnpm (2026-10-09)
## Working rules
- [g4] no Tailwind in this project (2026-10-07)
```

**Length.** If the digest goes over 3 000 tokens, code removes the oldest
lines of the last non-empty section, then the one before, until it fits.
A model cannot count tokens reliably, and a digest that grows would leave less
room for the draft, until a compaction ran at every turn. The removed lines are
already in Cognee, sent by this packet or an earlier one.

### Packet

The packet holds only what is new since the last compaction: the draft
entries, cleaned and merged, lessons included. The digest is not sent again;
it was sent piece by piece by earlier packets.

It is uploaded as a text file whose name is its identity in Cognee:

```
<repo name> <date> <time> <topics>.txt
acme-shop 2026-10-14 15h32 header grid, sharp.txt
```

The time keeps names unique: Cognee refuses a known name with different
content. If the name is already in the outbox, or Cognee answers 409 anyway,
` (2)`, ` (3)`… is added before `.txt`. Two clones with the same directory name
share the name prefix: an accepted limit.

### Calibration

On two or three real drafts, the default model is checked: the compacted
digest and packet must keep what the user said without changing it.
`tests/compare_compactors.py` runs the same draft through other models (Gemma 4
31B, for instance) for comparison.

## 6. Failures

Memory never blocks work, and no model runs for nothing. A service that keeps
failing is paused, and the agent tells the user.

### Counting failures

Each of the three services (Jev, the compactor, Cognee) has its own counter in
`state.json`, shared by every session on the machine.

| Result of a call | Effect |
|---|---|
| success | counter back to 0 |
| no answer, timeout, HTTP 5xx | counter + 1; at 3, the service is paused |
| HTTP 401 or 403 (key refused) | paused at once: retrying cannot help |
| HTTP 429 or 529 (rate limited, overloaded) | not counted: this turn goes without the service |
| any other HTTP 4xx | counter + 1, except a Cognee recall 404 meaning the dataset is empty, which is an empty result |
| compactor answer invalid twice (part 5) | counter + 1 |

```json
{
  "token_ratio": 3.7,
  "services": {
    "jev":       {"failures": 0, "paused": false, "reason": ""},
    "compactor": {"failures": 0, "paused": false, "reason": ""},
    "cognee":    {"failures": 3, "paused": true, "reason": "timeout", "since": "2026-10-14T09:12:40Z"}
  }
}
```

A pause covers every session on the machine at once, and one
`/memsift-resume` lifts it. memsift never resumes on its own.

### While paused

| Paused | What keeps running |
|---|---|
| Jev | nothing: without sorting, memsift has nothing to decide. Each turn is appended to `pending.jsonl` (part 2), without any model |
| compactor | everything but compaction. Notes keep growing; Jev reads the newest within its budget (part 3) |
| Cognee | everything on the local notes. The prompt hook never asks Cognee, `cognee-index.json` is not rebuilt, packets wait in the outbox |

### Telling the agent

At the next prompt of each session, the prompt hook adds one line for each
paused service, once per session (`outage_notices`):

```
memsift is paused: Jev stopped answering (3 failures: timeout). Memory is off.
Tell the user; they can run /memsift-resume once it is fixed.

memsift: Cognee stopped answering (3 failures). Memory keeps working from the
local notes; packets wait to be sent. Tell the user; /memsift-resume restarts it.

memsift: the compactor stopped answering (3 failures). Notes are no longer
compacted. Tell the user; /memsift-resume restarts it.
```

No system notification: the agent is the one channel that works on every host
without installing anything.

### Outbox

A packet that cannot be sent waits as one file per packet:

```
repos/<repo-key>/outbox/acme-shop 2026-10-14 15h32 header grid, sharp.txt
```

Sending is retried, oldest first, at session start (memsift already talks to
Cognee for the index), after each compaction, and on `/memsift-resume`. A file
is deleted once Cognee has accepted it.

### /memsift-resume

```
1. tests each paused service with one small call; a test never changes a
   counter, only a successful resume does
2. the ones that answer are resumed, counters back to 0;
   the others stay paused, and the summary says why
3. Cognee resumed → sends the outbox of every repo
4. Jev resumed    → sorts this repo's pending.jsonl now (one stop request per
                    turn, part 4), then deletes it
   every resumed service leaves the outage_notices of running sessions, so a
   later outage is announced again
5. prints a summary:
   Jev ✓ · 12 turns sorted, 3 kept · Cognee ✗ still unreachable (timeout)
```

Other repos' `pending.jsonl` are sorted by the first stop hook in that repo
once Jev is back, in the background: sorting needs the repo's own `jev.md`.

### /memsift-doctor

One line per check, `✓` or `✗` with what to do. Key values are never printed.
Its service checks are one small call each, paused or not, and never change a
counter.

```
✓ Python 3.12
✓ configuration found: COGNEE_BASE_URL, COGNEE_API_KEY, OPENROUTER_API_KEY
✓ Cognee reachable, key accepted, dataset memsift
✓ Jev reachable (one minimal request)
✗ compactor: HTTP 401, check OPENROUTER_API_KEY
✓ ~/.memsift writable
✓ nothing paused · 0 turns pending · 0 packets waiting
✗ datasets.md: "clients" has no description
✓ this repo: jev.md found
```

## 7. Configuration and tests

### Configuration

Read from the environment first, then from `~/.cognee/.env`, the file the
Cognee plugins already use. Values are never printed or logged.

| Variable | Default | Description |
|---|---|---|
| `COGNEE_BASE_URL` | required | URL of the Cognee server |
| `COGNEE_API_KEY` | required | Cognee API key, sent as `X-Api-Key` |
| `OPENROUTER_API_KEY` | required | key for Jev |
| `MEMSIFT_JEV_MODEL` | `typesafe/jev-1.13` | Jev model, pinned to a version (part 4) |
| `MEMSIFT_COMPACTOR_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible base URL |
| `MEMSIFT_COMPACTOR_API_KEY` | `OPENROUTER_API_KEY` | key for that endpoint; needed only for another endpoint |
| `MEMSIFT_COMPACTOR_MODEL` | `anthropic/claude-haiku-5.5` | compactor model at that endpoint |
| `MEMSIFT_DATASET` | `memsift` | the only Cognee dataset memsift writes to |
| `MEMSIFT_DEBUG` | `0` | `1` writes every decision to `debug.log` |

Thresholds, sizes and timeouts are not settings: they live in the code, one
place per module, and change only with calibration.

### debug.log

`~/.memsift/debug.log`, bounded by `vendor/logfiles.py`: past 20 MB it is
rotated once (`debug.log.1`), so it never holds more than 40 MB. It never
leaves the machine, and it is redacted like everything else.

**Errors are always written**, whatever `MEMSIFT_DEBUG` says: every exception
with its full traceback, every failed call with its service, HTTP status,
duration and the first 300 characters of the response, every invalid
compactor answer. memsift never fails the host, so this file is where a
failure must show.

```
09:58:31 ERROR stop    compactor: answer invalid (digest empty, notes had 14 lines), retry 1/1
09:58:44 ERROR stop    compactor: answer invalid again (missing "packet") → nothing written · failures 1/3
10:02:10 ERROR prompt  cognee: timeout after 12.0 s · failures 2/3
10:04:55 ERROR tool    KeyError: 'tool_input'
                       Traceback (most recent call last): …
```

**Decisions are written with `MEMSIFT_DEBUG=1`**, one line each, with timings
and token counts:

```
09:41:02 prompt  "back to the header"  missing 0.85 · g1 0.92 → 1 entry pasted
                 jev 0.4 s · 21 300 tokens (estimated 22 100)
09:41:40 stop    decision · this task · 0.81 → d7 appended
09:52:13 prompt  "pick up the footer from two weeks ago"  missing 0.95 · cognee 0.90
                 → Cognee lessons, memsift 5.8 s → p2 kept · total 6.6 s
```

### Automatic tests

`python3 -m unittest discover -s tests`, standard library only, no network,
no model. Each test runs with a temporary `HOME`, so it never touches the real
`~/.memsift` or `~/.cognee`.

```
tests/
├── fake_services.py      a local HTTP server playing Jev, the compactor and
│                         Cognee from scripted answers: probabilities, a delay,
│                         HTTP 401 / 429 / 500, an invalid JSON, a timeout;
│                         it records every request it receives
├── fixtures/
│   ├── hooks/            real hook JSON shapes, Claude Code and Codex, one per
│   │                     event and tool
│   └── transcripts/      invented acme-shop conversations in both transcript
│                         formats, with and without compaction; never real
│                         sessions
└── test_*.py
```

What each module checks:

| Module | Cases |
|---|---|
| `test_store` | two processes appending 200 entries each to one `draft.md`: no line lost or interleaved · a rewrite under lock is never seen half-written · cuts at 1 500 / 1 000 tokens keep start and end with `[…]` · two repos with the same name get different keys · outside git the working directory is the repo · missing files read as empty |
| `test_redact` | API keys, tokens, private keys and `PASSWORD=`-style values are masked in `draft.md`, `pending.jsonl`, `debug.log`, the outbox, and in every request the fake server records |
| `test_transcript` | Claude Code: only the last summary after the last marker is read; tool calls replaced by action notes · Codex: the encrypted summary is skipped, the user messages kept in clear are read · the over-budget cut order of part 3 · a transcript of several MB is read from the end, not loaded whole |
| `test_actions` | in a temporary git repo: an edit in the repo is noted with a diff of at most 200 characters · a write keeps the file name only · edits outside the repo are ignored · a read-only command is ignored · a command changing an already modified file is noted · a commit is noted · outside git, commands are not noted · failed then succeeded is marked · Codex `apply_patch` is noted like an edit · only the last 5 turns are kept |
| `test_decisions` | table-driven, probabilities in, outcome out: the prompt cascade (thresholds and their boundaries, dataset fallback to the highest) · the passage rule · the stop rule, including the lower bar for corrections · confirmed corrections · each flag given once, then removed |
| `test_compaction` | lines appended during a compaction survive it · pending corrections are carried over · an invalid answer twice writes nothing and counts one failure · an empty digest from notes that had lines is refused, retried and logged with its reason · an over-long digest loses its oldest lines of the last section · lines are numbered `g1`… by code · a second compaction finding the lock does nothing · packet names carry the time, and a taken name gets ` (2)` · the packet is in the outbox before `digest.md` is rewritten · Cognee down leaves the packet in the outbox · a draft of pending corrections only makes no model call · a lessons-only draft may give an empty digest · a confirmation appended during a compaction is carried over with its correction |
| `test_failures` | 3 timeouts pause a service · 401 pauses at once · 429 and 529 are not counted · a success resets the counter · the notice is given once per session · a pause seen by one session applies to all · `/memsift-resume` tests, resumes, sends the outbox, sorts this repo's `pending.jsonl` and prints its summary · another repo's pending turns are sorted by its first stop · a stop whose Jev call times out queues its turn in `pending.jsonl` · two sorters on one repo sort each turn once · a test call during resume or doctor changes no counter |
| `test_hooks` | `run.py` end to end for every event and host, against the fake server: output shape per host · background hooks return in under a second · the prompt hook returns nothing within about 2 s when Jev takes 3 s · an exception inside the core still exits 0 and is in `debug.log` with its traceback |
| `test_doctor` | each check reports ✓ and ✗ correctly · no key value appears in the output |

**Continuous integration.** `.github/workflows/tests.yml` runs the suite on
each push and pull request, on Python 3.9 (the minimum) and the latest stable
Python.

### Calibration

Calibration calls real models, so each script first prints its estimated cost
and waits for confirmation. Labelled data stays outside the repo.

- `tests/calibrate_jev.py <labels.jsonl>`: each line holds a prompt, the notes
  and conversation it came with, and the expected outcome (`"nothing"`,
  `{"entries": [...]}` or `{"cognee": true}`). Optional `datasets` (name →
  description and items) and `session_start`; without them, one dataset
  `memory` described as past work, decisions and lessons of the project, and a
  new session. Prints the thresholds that match the labels best, how
  often Cognee would be asked, and the checks listed in part 4.
- `tests/compare_compactors.py <draft.md>`: compacts the same draft with each
  model of part 5 and prints the results side by side, for a person to judge
  fidelity.

### Live tests

With `MEMSIFT_DEBUG=1` and `tail -f ~/.memsift/debug.log` in a second pane,
first on Claude Code, then on Codex:

| # | Do | Expect |
|---|---|---|
| 1 | new session, "back to the header" | the digest entry about the header is pasted |
| 2 | "fix the typo in the README" | nothing is pasted |
| 3 | correct the agent, then let it commit | `confirmed` appended for that correction |
| 4 | ask about a topic two weeks old | Cognee asked; waiting time measured |
| 5 | state a structuring decision without a reason | the agent asks why at the next prompt |
| 6 | fill the draft | compaction runs; a packet reaches Cognee |
| 7 | set a wrong Cognee key | pause after the first call, one line to the agent, `/memsift-resume` after fixing |
| 8 | `/compact`, then ask about something from before | Jev reads the summary; nothing redundant pasted |
