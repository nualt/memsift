# memsift

Memory for coding agents (Claude Code, Codex) that gives the agent only what
it is missing. Most of the time, that is nothing.

Memory plugins for coding agents usually recall on every prompt: they query a
knowledge graph, take the passages closest to the words you typed, and paste
them into the context. The agent ends up with a few thousand characters it did
not ask for, half of which it already had, after waiting several seconds for
them, even when you only typed "ok, go ahead". memsift puts a small decision
model in front of the memory. It asks whether the agent is actually missing
something that already exists, where to find it, and whether each recalled
passage would change what the agent does next. Only what survives is added.

It was built for the agents that run [Nualt](https://nualt.fr)'s own projects,
on a self-hosted [Cognee](https://github.com/topoteretes/cognee) server, as a
fork of Cognee's own Claude Code and Codex integrations.

> Status: 0.1.1. Tested offline and in real Claude Code sessions on two scripted
> test projects (100 and 80 prompts), against the original plugin. Codex is
> tested offline only. The contract is in [docs/SPEC.md](docs/SPEC.md).

## What is in here

```
.claude-plugin/      Claude Code manifest and marketplace
.codex-plugin/       Codex manifest
.agents/plugins/    Codex marketplace, plugin path ./
hooks/               one hook file per host, both calling the same code
commands/            Claude Code: /memsift-resume, /memsift-doctor
skills/              Codex: memsift-resume/SKILL.md, memsift-doctor/SKILL.md
memsift/             the code, Python standard library only
tests/               isolated offline tests and opt-in calibration tools
.github/workflows/   offline tests on Python 3.9 and stable 3.x
docs/SPEC.md         what memsift does, precisely enough to implement and test
docs/ARCHITECTURE.md the full picture, and why it is built this way
LICENSE, NOTICE      Apache-2.0, and the credit to the upstream project
```

## How it works

memsift hooks into five moments of a session. Only the prompt makes you wait,
and only the prompt adds anything to the agent's context.

```
  you type a prompt
      │
      ▼
  Jev, two seconds at most: what kind of prompt is it?
      │
      ├── needs nothing from memory (most prompts:
      │   "ok", "commit", a self-contained task) ──► nothing is added
      │
      ├── may need memory, and a note answers ────► that note is added
      │   (each note checked on its own)
      │
      └── no note answers, and the topic is the kind
          memsift keeps (a technical choice, a constraint,
          a problem met before)
              │
              ▼
          Cognee returns its closest passages (12 s at most)
              │
              ▼
          Jev, two seconds at most: keeps the passages that answer
          the prompt, each checked on its own
              │
              ▼
          a few lines are added, often none
```

1. **Session start.** memsift notes how the session started, checks that the
   project has a `jev.md`, and in the background fetches the list of what
   Cognee holds. Nothing is sent to the agent.
2. **Each prompt.** Jev sorts the prompt into one of eleven kinds: a reaction,
   a self-contained task or a general question needs nothing from memory; a
   question about a past choice, resuming work, a problem coming back or a
   task that builds on what is already set up does. In that case it checks
   each note on its own against the prompt, and if none answers, whether the
   topic is the kind memsift keeps, which decides if Cognee is asked (and in
   which datasets). Cognee's passages are checked one by one before anything
   is added.
3. **While the agent works.** memsift notes what changed the repo: edits,
   commits, a command that failed and then worked. Reads are ignored. Nothing
   leaves the machine.
4. **After each answer.** Jev decides, in the background, whether the turn
   taught anything worth keeping. A correction stays pending until later turns
   confirm it; compaction applies confirmed corrections.
5. **Session end.** Whatever is left in the notes is compacted and sent to
   Cognee, in the background. A failed upload stays in the outbox.

Jev sorts each turn into one of three kinds:

| Kind | Example | Where it goes |
|---|---|---|
| this project | "no Tailwind in this repo", "we go with Stripe: PayPal fees are too high", "prices are shown with VAT" | the project's working notes, then Cognee |
| a technical lesson | "pnpm blocks build scripts unless allowed" | the working notes until compaction, then Cognee only |
| nothing | "ok", typos, status updates, a task done once, UI details such as layout or colour (they belong in a design system) | dropped |

The line is one test: would knowing it save redoing the whole path if the same
situation came back tomorrow?

How you like to work in general ("ask before committing") is not kept: it
belongs in your `CLAUDE.md` or `AGENTS.md`, which the agent already loads in
full. memsift keeps project memory only.

When the working notes reach 8 000 tokens, a cheap model (Claude Haiku, by default) cleans and
deduplicates them, sends them to Cognee, and updates a short digest of the
project: it adds every new project entry and names the old lines to replace or
drop; every other line is kept word for word. An option only considered ("I was
thinking of Netlify") is kept, marked as considered; a later decision replaces
it. At the end of a session, whatever is left in the notes goes the same way.
When the digest reaches 3 000 tokens, it is emptied but for its working rules:
everything else is already in Cognee.

The full picture, moment by moment, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Why it is built this way

1. **A decision model, not a chat model, decides what to inject.** Jev returns
   probabilities, not text. A typical full prompt budget is about 26 000
   estimated tokens. Each prompt classification has a two-second deadline;
   the whole Cognee cascade has at most 2 + 12 + 2 = 16 seconds. On a scripted
   80-prompt project, memory was added on 13 prompts, after a median wait of
   0.8 s; the original plugin added some on all 80, after 3.0 s.
2. **Missing means it already exists somewhere.** When you are writing a spec,
   everything is new and nothing is in memory yet. Asking Cognee then is wasted
   time. Jev looks for information that exists, not information the agent
   should invent.
3. **The repo is the memory of what is in it.** Code, docs and `CLAUDE.md` are
   not stored again. A choice that can be read in the code ("the button is
   blue") is not a decision worth remembering.
4. **Corrections wait for proof.** When you correct the agent, the old line is
   only replaced once an edit, a commit or the following turns confirm it.
5. **Local first, Cognee second.** Jev selects entries from the working notes
   before considering Cognee, the long-term store.
6. **One source of truth per thing.** General working rules stay in the
   host's own instruction files; memsift does not keep a second copy. The list
   of what Cognee holds is a cache rebuilt each session, never edited by hand.
7. **Stop instead of failing quietly.** After three failures in a row, memsift
   pauses that service and the agent tells you at its next prompt.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) gives the full reasoning.

## Files on disk

```
~/.memsift/
├── datasets.md             what each Cognee dataset is for, one line each
├── state.json              failures and pauses, per service, for the whole machine
├── debug.log               every error, always; every decision when MEMSIFT_DEBUG=1
├── sessions/<session>.json one per running session, deleted at session end
└── repos/<repo>/
    ├── digest.md           compacted project memory (≤ 3 000 tokens)
    ├── draft.md            turns kept by Jev since the last compaction
    ├── pending.jsonl       turns noted while Jev was unreachable
    ├── outbox/             packets waiting for Cognee
    ├── compaction.lock     held while a compaction runs
    ├── replay.lock         held while pending turns are sorted and acknowledged
    └── cognee-index.json   what Cognee holds, refreshed each session

<repo>/jev.md               a short project card: what it is, what matters, which phase
```

The working notes are `digest.md` and `draft.md` read together. At 8 000
estimated tokens they trigger compaction; they can grow while it is paused.
Every session on the machine reads the same notes for its repo, so a
decision noted in one pane is seen at the next prompt of another pane on the
same repo. Writes are append-only; compaction runs under a lock.

`jev.md` is the one file you own. memsift proposes a first draft when it is
missing and flags it when it looks outdated, but never edits it without you.

## Installation

Requirements:

- Python 3.9 or later as `python3`; standard library only, nothing to install
- a running Cognee server and its API key
- an [OpenRouter](https://openrouter.ai) key, for Jev and, by default, the
  compactor; any other OpenAI-compatible endpoint can run the compactor

Claude Code:

```
claude plugin marketplace add nualt/memsift
claude plugin install memsift@memsift
```

Codex:

```
codex plugin marketplace add nualt/memsift --ref main
codex plugin add memsift@memsift
```

These commands match the locally checked host CLI syntax. Marketplace
installation, Codex's root source `./`, skill discovery and prompt-context
delivery still require live verification.

Then put your keys in `~/.cognee/.env`, start a new session and run
`/memsift-doctor` in Claude Code or `$memsift-doctor` in Codex (or ask for the
skill). It checks the configuration and each service with one minimal request,
even when paused, and says what to fix. Doctor does not resume services.

## Configuration

Settings are read from the environment, then from `~/.cognee/.env`.
[docs/SPEC.md](docs/SPEC.md), part 7, is the reference.

| Variable | Default | Description |
|---|---|---|
| `COGNEE_BASE_URL` | none (required) | URL of your Cognee server |
| `COGNEE_API_KEY` | none (required) | Cognee API key, sent as `X-Api-Key` |
| `OPENROUTER_API_KEY` | none (required) | key for Jev; usage shows on your OpenRouter dashboard |
| `MEMSIFT_JEV_MODEL` | `typesafe/jev-1.13` | Jev model on OpenRouter |
| `MEMSIFT_COMPACTOR_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible base URL |
| `MEMSIFT_COMPACTOR_API_KEY` | `OPENROUTER_API_KEY` | key for that endpoint; needed only for another endpoint |
| `MEMSIFT_COMPACTOR_MODEL` | `anthropic/claude-haiku-5.5` | compactor model id at that endpoint |
| `MEMSIFT_DATASET` | `memsift` | the Cognee dataset memsift writes to; it never writes elsewhere |
| `MEMSIFT_DEBUG` | `0` | `1` also writes every decision to `~/.memsift/debug.log`; errors are always written |

## What leaves your machine

| Sent to | What | When |
|---|---|---|
| OpenRouter (Jev) | prompt classification: your prompt, the end of the conversation since the last compaction (up to 25 000 tokens), each working-notes entry, dataset descriptions and item names; Stop: the turn's prompt, answer and action notes, recent actions, `jev.md`, notes, pending corrections and repo-root `CLAUDE.md` / `AGENTS.md`, plus recent exchanges when corrections are pending; passage selection: your prompt and each recalled passage | each prompt and after each answer, while Jev is active |
| the compactor | one project's digest and whole draft, including pending corrections, with compaction instructions and a response schema | at 8 000 estimated tokens, at session end, or a minimal doctor/resume probe |
| Cognee | recall: the current redacted prompt as the query, selected dataset IDs/names and search metadata; upload: cleaned packet text, packet filename, target dataset and node-set fields; index: requests for dataset and item lists | recall only when Jev selects it; uploads after compaction, at session start and on resume; index at session start; minimal doctor/resume probes |

Model requests also carry the configured model ID and the questions or
instructions. Each service receives its API key in an authentication header;
redaction applies to content, not to the key needed to authenticate the call.

Secrets are redacted before anything is sent: API keys, tokens, private keys,
and values of variables whose names look like credentials. Generated local
files and temporary worker inputs are redacted before writing too. Tool output is never
sent as is; memsift only keeps a short note of what changed.

Codex compaction summaries are encrypted. memsift uses the user messages kept
in clear in the compacted record, so it can sometimes inject something the
agent already retained. Claude Code uses the latest clear summary.

## Cost

Measured on OpenRouter over one day of tests (about 200 prompts, plus
checks): about $0.04 in all, $0.03 for Jev and $0.01 for the compactor.
Cognee runs on your own server. Cost depends on provider prices and on how
often memory is selected. The opt-in calibration scripts print estimates from
their dated price table and ask for confirmation before calling models.

## Watching what it does

Errors are always written to `~/.memsift/debug.log`, with their traceback:
memsift never breaks your agent, so this is where a failure shows. If the log
itself is physically unwritable, the runner still returns zero without context
and cannot record that failure there. With
`MEMSIFT_DEBUG=1`, every decision is written too:

```
tail -f ~/.memsift/debug.log

DECISION core-prompt {"entries": ["g1"], "datasets": [], "recall": false}
DECISION jev-tokens {"estimated_tokens": 21300, "input_tokens": 21100}
ERROR core-prompt cognee status=0 duration=12.000
```

These are illustrative records, not measured latency. The log rotates at
20 000 000 bytes and keeps one previous file.

## When something fails

| Down | What happens |
|---|---|
| Jev | failed Stop calls queue turns immediately; once paused, automatic model work stops and turns keep queuing |
| the compactor | Jev keeps working; compaction waits |
| Cognee | Jev keeps working from the local notes; packets wait to be sent |

Three consecutive failures trigger a pause; HTTP 401/403 triggers it at once.
HTTP 429/529 is logged but does not increment or reset the counter. A successful
ordinary call resets failures without lifting a pause. Each paused service is
announced once per running session at its next prompt.

Resume explicitly with `/memsift-resume` in Claude Code or `$memsift-resume`
in Codex (or ask for the skill). Successful probes clear that service's pause
and counter and allow later outages to be announced again. Resume drains every
repo's outbox and sorts pending turns in the current repo. Other repos replay
at their next successful Stop; this repo can also replay at a successful Stop.
There is no automatic resume. On a prompt timeout, work continues without memory.

## Hosts

| Host | Status |
|---|---|
| Claude Code | tested offline and in real sessions |
| Codex | tested offline |
| Gemini CLI | later; its hooks can inject context |
| Cursor | later, partial; its prompt hook cannot inject context |

## Status

Run the isolated suite with `python3 -m unittest discover -s tests -t .`.
It uses invented `acme-shop` data, temporary HOME directories and a fake HTTP
server on `127.0.0.1`; external sockets are refused in tests and their guarded
children. No dependencies, real credentials or model calls are needed. CI is
configured for Python 3.9 and the latest stable `3.x`, with prereleases disabled.

Live runs: two scripted test projects (100 and 80 prompts) in real Claude Code
sessions, with real models and a real Cognee server, each also run with the
original plugin. Corrections confirmed over several turns, `/compact` and
service outages are covered by offline tests only. The thresholds were set on
47 labelled real prompts; the calibration scripts (`tests/calibrate_jev.py`,
`tests/compare_compactors.py`) print their cost estimate and ask before
calling models. Keep labels and captures outside the repository. Reports of
wrong injections and missed recalls are welcome as issues.

## Origin and license

memsift is a fork of the Claude Code and Codex integrations from
[topoteretes/cognee-integrations](https://github.com/topoteretes/cognee-integrations)
(Claude Code plugin 1.6.6, Codex plugin 1.7.5), kept with its git history and
modified substantially. It is not affiliated with or endorsed by Cognee.

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
