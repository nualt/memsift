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

> Status: specification in progress, not usable yet. The design is fixed in
> [docs/SPEC.md](docs/SPEC.md); this README describes what 0.1 will do.

## What is in here

```
.claude-plugin/      Claude Code manifest and marketplace
.codex-plugin/       Codex manifest
hooks/               one hook file per host, both calling the same code
commands/            /memsift-resume, /memsift-doctor
memsift/             the code, Python standard library only
tests/               offline tests, with Jev, the compactor and Cognee simulated
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
  Jev, two seconds at most: is the agent missing something
  that already exists?
      │
      ├── no (most prompts) ──────────────────────► nothing is added
      │
      ├── yes, it is in this project's notes ─────► those entries are added
      │
      └── yes, and it is worth asking Cognee
              │
              ▼
          Cognee returns its closest passages (12 s at most)
              │
              ▼
          Jev keeps the ones that change what the agent will do
              │
              ▼
          a few lines are added, often none
```

1. **Session start.** memsift notes how the session started, checks that the
   project has a `jev.md`, and in the background fetches the list of what
   Cognee holds. Nothing is sent to the agent.
2. **Each prompt.** Jev answers in one request: is anything missing, which
   entries of the working notes hold it, and is it worth asking Cognee (and in
   which datasets). Cognee is only queried when Jev is confident it will help,
   and Jev sorts its passages before anything is added.
3. **While the agent works.** memsift notes what changed the repo: edits,
   commits, a command that failed and then worked. Reads are ignored. Nothing
   leaves the machine.
4. **After each answer.** Jev decides, in the background, whether the turn
   taught anything worth keeping. A correction waits until later turns confirm
   it.
5. **Session end.** Whatever is left in the notes is compacted and sent to
   Cognee, in the background.

| Kind | Example | Where it goes |
|---|---|---|
| a decision | "the header reuses the footer grid" | the project's working notes, then Cognee |
| a project fact | "sharp must be allowed in pnpm here" | the project's working notes, then Cognee |
| a rule for this project | "no Tailwind in this repo" | the project's working notes, then Cognee |
| a technical lesson | "pnpm blocks build scripts unless allowed" | the working notes until compaction, then Cognee only |
| nothing | "ok", typos, status updates | dropped |

How you like to work in general ("ask before committing") is not kept: it
belongs in your `CLAUDE.md` or `AGENTS.md`, which the agent already loads in
full. memsift keeps project memory only.

When the working notes reach 8 000 tokens, a cheap model (Claude Haiku, by default) cleans and
deduplicates them, sends them to Cognee, and rewrites a short digest of the
project. At the end of a session, whatever is left goes the same way.

The full picture, moment by moment, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Why it is built this way

1. **A decision model, not a chat model, decides what to inject.** Jev returns
   probabilities, not text. It reads up to 26 000 tokens of context and is
   announced to answer in under half a second, for a fraction of a cent. That is what makes it
   affordable to ask on every prompt.
2. **Missing means it already exists somewhere.** When you are writing a spec,
   everything is new and nothing is in memory yet. Asking Cognee then is wasted
   time. Jev looks for information that exists, not information the agent
   should invent.
3. **The repo is the memory of what is in it.** Code, docs and `CLAUDE.md` are
   not stored again. A choice that can be read in the code ("the button is
   blue") is not a decision worth remembering; the reason behind it might be.
4. **Corrections wait for proof.** When you correct the agent, the old line is
   only replaced once an edit, a commit or the following turns confirm it.
5. **Local first, Cognee second.** The working notes answer most questions in
   milliseconds. Cognee is the long-term store, queried on purpose.
6. **One source of truth per thing.** General working rules stay in the
   host's own instruction files; memsift does not keep a second copy. The list
   of what Cognee holds is a cache rebuilt each session, never edited by hand.
7. **Stop instead of failing quietly.** After three failures in a row, memsift
   pauses and the agent tells you, rather than running models in the void.

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
    └── cognee-index.json   what Cognee holds, refreshed each session

<repo>/jev.md               a short project card: what it is, what matters, which phase
```

The working notes are `digest.md` and `draft.md` read together, up to 8 000
tokens. Every session on the machine reads the same files on each hook, so a
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

Then put your keys in `~/.cognee/.env`, start a new session and run
`/memsift-doctor`: it checks the configuration and that Cognee, Jev and the
compactor answer, and says what to fix.

## Configuration

Settings are read from the environment, then from `~/.cognee/.env`.
[docs/SPEC.md](docs/SPEC.md), part 7, is the reference.

| Variable | Default | Description |
|---|---|---|
| `COGNEE_BASE_URL` | none (required) | URL of your Cognee server |
| `COGNEE_API_KEY` | none (required) | Cognee API key, sent as `X-Api-Key` |
| `OPENROUTER_API_KEY` | none (required) | key for Jev; usage shows on your OpenRouter dashboard |
| `MEMSIFT_JEV_MODEL` | `typesafe/jev-1.13` | Jev model on OpenRouter |
| `MEMSIFT_COMPACTOR_URL` | OpenRouter | any OpenAI-compatible base URL |
| `MEMSIFT_COMPACTOR_API_KEY` | `OPENROUTER_API_KEY` | key for that endpoint; needed only for another endpoint |
| `MEMSIFT_COMPACTOR_MODEL` | `anthropic/claude-haiku-5.5` | compactor model id at that endpoint |
| `MEMSIFT_DATASET` | `memsift` | the Cognee dataset memsift writes to; it never writes elsewhere |
| `MEMSIFT_DEBUG` | `0` | `1` also writes every decision to `~/.memsift/debug.log`; errors are always written |

## What leaves your machine

| Sent to | What | When |
|---|---|---|
| OpenRouter (Jev) | your prompt, the recent exchanges and last compaction summary, the working notes, `jev.md`, the dataset descriptions and item names; after each answer, also the agent's answer, the action notes and the repo's `CLAUDE.md` / `AGENTS.md` | each prompt and after each answer |
| the compactor | the working notes of one project | when they are full, and at session end |
| Cognee | the cleaned notes; a request for the list of datasets and items | at compaction; the list at session start |

Secrets are redacted before anything is sent: API keys, tokens, private keys,
and values of variables whose names look like credentials. Tool output is never
sent as is; memsift only keeps a short note of what changed.

## Cost

At the time of writing, Jev costs $0.042 per million input tokens. A prompt
with a full context (about 26 000 tokens) costs about a tenth of a cent; most
prompts carry much less. A compaction with Claude Haiku 5.5 through
OpenRouter ($0.10 in, $0.50 out per million tokens) costs about half a cent,
a few times a day. Cognee runs
on your own server.

## Watching what it does

Errors are always written to `~/.memsift/debug.log`, with their traceback:
memsift never breaks your agent, so this is where a failure shows. With
`MEMSIFT_DEBUG=1`, every decision is written too:

```
tail -f ~/.memsift/debug.log

09:41:02 prompt  "back to the header"  missing 0.85 · g1 0.92 → 1 entry pasted
09:41:40 stop    decision · this task · 0.81 → d7 appended
10:02:10 ERROR prompt  cognee: timeout after 12.0 s · failures 2/3
```

## When something fails

| Down | What happens |
|---|---|
| Jev | memsift pauses entirely; turns are queued in `pending.jsonl` and sorted on resume |
| the compactor | Jev keeps working; compaction waits |
| Cognee | Jev keeps working from the local notes; packets wait to be sent |

Three failures in a row trigger the pause; a refused key triggers it at once.
The agent is told once, in one line,
and you resume with `/memsift-resume`. On a prompt, Jev gets two seconds; past
that, the prompt goes through without memory.

## Hosts

| Host | Status |
|---|---|
| Claude Code | planned for 0.1 |
| Codex | planned for 0.1 |
| Gemini CLI | later; its hooks can inject context |
| Cursor | later, partial; its prompt hook cannot inject context |

## Status

memsift is the memory layer we use at Nualt, on our own projects, with one
Cognee server. The probability thresholds are first guesses, to be calibrated
on about fifty real prompts once the code runs. Reports of wrong injections and
missed recalls are welcome as issues.

## Origin and license

memsift is a fork of the Claude Code and Codex integrations from
[topoteretes/cognee-integrations](https://github.com/topoteretes/cognee-integrations)
(Claude Code plugin 1.6.6, Codex plugin 1.7.5), kept with its git history and
modified substantially. It is not affiliated with or endorsed by Cognee.

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
