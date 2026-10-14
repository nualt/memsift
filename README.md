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
commands/            /memory-resume
memsift/             the code, Python standard library only
tests/               offline tests, with Jev, the compactor and Cognee simulated
docs/SPEC.md         what memsift does, precisely enough to implement and test
docs/ARCHITECTURE.md why it is built this way
LICENSE, NOTICE      Apache-2.0, and the credit to the upstream project
```

## How it works

memsift hooks into four moments of a session. None of them blocks you for long,
and only the first one adds anything to the agent's context.

```
  you type a prompt
      │
      ▼
  Jev: is the agent missing something that already exists?
      │
      ├── no (most prompts) ──────────────────────► nothing is added
      │
      ├── yes, it is in this project's notes ─────► those lines are added
      │
      └── yes, and it is worth asking Cognee
              │
              ▼
          Cognee returns its closest passages
              │
              ▼
          Jev keeps the ones that change what the agent will do
              │
              ▼
          a few lines are added, often none
```

1. **Session start.** memsift fetches the list of what Cognee holds and checks
   that the project has a `jev.md`. Nothing is sent to the agent.
2. **Each prompt.** Jev answers three questions in one request: is anything
   missing, is it in the working notes, and is it worth asking Cognee (and in
   which datasets). Cognee is only queried when Jev is confident it will help.
3. **While the agent works.** memsift notes what changed the repo: edits,
   commits, a command that failed and then worked. Reads are ignored. Nothing
   leaves the machine.
4. **After each answer.** Jev decides, in the background, whether the turn
   taught anything worth keeping, and where it belongs.

| Kind | Example | Where it goes |
|---|---|---|
| this project | "the header reuses the footer grid" | the project's working notes, then Cognee |
| a rule for this project | "no Tailwind in this repo" | the project's working notes, then Cognee |
| a technical lesson | "pnpm blocks build scripts unless allowed" | Cognee only |
| nothing | "ok", typos, status updates | dropped |

How you like to work in general ("ask before committing") is not kept: it
belongs in your `CLAUDE.md` or `AGENTS.md`, which the agent already loads in
full. memsift keeps project memory only.

When the working notes reach 8 000 tokens, a cheap model (Gemma) cleans and
deduplicates them, sends them to Cognee, and rewrites a short digest of the
project. At the end of a session, whatever is left goes the same way.

<details>
<summary>The full picture</summary>

```
  WHAT JEV HAS AT HAND  (almost free, never pasted as is into the agent)
  ┌────────────────┐ ┌────────────────┐ ┌──────────────────────┐ ┌────────────────┐
  │ jev.md         │ │ CLAUDE.md      │ │ what the agent has   │ │ the repo's     │
  │ project, what  │ │ "already       │ │ in mind: last        │ │ WORKING NOTES  │
  │ matters, phase │ │ written" (③)   │ │ /compact + recent    │ │ digest + draft │
  └────────────────┘ └────────────────┘ └──────────────────────┘ └────────────────┘


 ⓪ SESSION START ─────────────────────────────────────────────── hook SessionStart
     │
     ├─ nothing to load: the WORKING NOTES are this repo's digest.md
     │  (compacted) + draft.md (since then), read as they are
     ├─ note how the session starts (new, /clear, /compact, resume)
     │  → Jev will know whether the agent starts from scratch
     ├─ Cognee: list of what it holds ──► cognee-index.json
     │  (in the background, ~1-3 s, rewritten each session)
     ├─ Cognee datasets compared to datasets.md: a new one?
     │  ──► flag "propose a description"
     └─ no jev.md?  ──► flag "propose a first draft"


 ① YOU TYPE A PROMPT ─────────────────────────────────────── hook UserPromptSubmit
     │
     ▼
   ┌─────────────── JEV · one request · ~0.1-0.5 s ───────────────┐
   │ ① "Is the agent missing something that ALREADY EXISTS?"      │
   │ ② "Is it in the WORKING NOTES?"  → if so, which lines        │
   │ ③ "Is it worth asking COGNEE?"                               │
   │    (is the topic in cognee-index.json?)                      │
   │    if so: in which datasets (read from datasets.md)          │
   │ → each answer comes with a probability (0 to 1)              │
   └───────────────────────────────┬──────────────────────────────┘
          ┌────────────────────────┴─────────────────────────┐
          ▼                        ▼                         ▼
   nothing missing       it is in the notes          not in the notes,
   (most prompts)        → the useful lines          and Jev ≥ 80 % sure
   → NOTHING ADDED         are added                 (otherwise: nothing)
                           │                                   │
                           │                                   ▼
                           │                         COGNEE: we wait
                           │                         (~6 s, 12 s max)
                           │                         it returns its 5
                           │                         passages CLOSEST to
                           │                         the question (not the
                           │                         most useful ones)
                           │                                   │
                           │                                   ▼
                           │               ┌────────────────── JEV ───────────────────┐
                           │               │ ④ for each passage:                      │
                           │               │    already in front of it? → dropped     │
                           │               │    off topic / useless?    → dropped     │
                           │               │    changes its next move?  → kept        │
                           │               └───────────────────┬──────────────────────┘
                           │                                   │
                           └───────────────────────────────────┤
                                                               ▼
   ┌────────────────────────────── AGENT CONTEXT ───────────────────────────────┐
   │ … our conversation …                                                       │
   │ [ what the sorting kept, often nothing. For example:                       │
   │   "Decision: the header reuses the footer grid" ]                          │
   │ [ if flagged: "jev.md looks outdated, propose an update" ]                 │
   │ your prompt                                                                │
   └────────────────────────────────────────────────────────────────────────────┘

   WAIT   usual plugin: ~6 s on EVERY prompt (median, up to 12 s)
          memsift     : ~0.1-0.5 s when the notes are enough,
                        a few seconds only when Jev judges
                        that Cognee is worth it


 ② THE AGENT WORKS ───────────────────────────────────────────── hook PostToolUse
     │   nothing is sent anywhere
     ▼
   ┌────────────── ACTION NOTE · local, plain code ───────────────┐
   │ Bash that reads (git status, ls…)      ✗ ignored             │
   │ Bash that changes (git commit…)        ✔ noted               │
   │ Edit / Write in the repo               ✔ noted               │
   │ Edit / Write in /tmp, scratch dirs     ✗ ignored             │
   │ Python script via Bash                 ✔ if the repo changed │
   │ failure, then success                  ✔ marked "lesson?"    │
   └──────────────────────────────────────────────────────────────┘


 ③ THE AGENT ANSWERED ─────────────────────────── hook Stop · in the background
     │   your prompt + its answer + action note
     ▼
   ┌──────────────────────────── JEV ─────────────────────────────┐
   │ "What kind of information is this?"                          │
   │   nothing important · this project · a technical lesson      │
   │ "Already known?"  in the notes, CLAUDE.md                    │
   │ "Is jev.md outdated?"                                        │
   └──────────────────────────────┬───────────────────────────────┘
     ┌────────────────────────────┴───────────────────────────┐
     ▼                            ▼                           ▼
   NOTHING                  PROJECT, LESSON             JEV.MD TO REVIEW
   "commit pls"             "header: footer grid"       → FLAG, added to
   "ask before              "no Tailwind here"            the next prompt:
     committing"            "pnpm blocks build            the agent proposes,
   → dropped                  scripts"                    you approve
                            → repo draft.md
                                  │
                                  ▼
                    notes full?  (digest + draft ≥ 8 000 tokens)
                    no → carry on
                                  │ yes
                                  ▼
   ┌───────────────── COMPACTION · in the background ─────────────────┐
   │ GEMMA reads ALL of the repo's notes, in one pass:                │
   │   ├─ cleans, deduplicates ──────────► SENDS TO COGNEE            │
   │   │    technical lessons go there only: Jev will                 │
   │   │    fetch them the day the problem comes back                 │
   │   └─ rewrites digest.md (≤ 3 000 tokens), empties draft.md       │
   │        this project: decisions, work in progress                 │
   │        priority: work in progress, then most recent              │
   │                                                                  │
   │ meanwhile, Jev keeps using the old notes                         │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
          NEXT TURN: Jev reads the new digest.md
          + draft.md, holding only the turns added
            during compaction

   LONG SESSIONS: each compaction rewrites digest.md with the new turns
   → never more than 3 000 tokens.
   What drops out of digest.md is not lost: it is already in Cognee.

   SESSION END: Gemma sends what is left in the notes to Cognee,
   even if they are not full, and rewrites digest.md for the next ⓪.

══════════════════════════════════════════════════════════════ COGNEE SERVER ═══

   ┌─────────────────── COGNEE · memsift dataset ────────────────────┐
   │ receives only clean, deduplicated packets                        │
   │ stores and links them (graph)                                    │
   │ → queried only at ①, when Jev judges it worth it                 │
   └──────────────────────────────────────────────────────────────────┘

   ┌─────────────────── YOUR CURATED KNOWLEDGE ───────────────────┐
   │ your other datasets: playbooks, projects, lessons…           │
   │ read at ① when Jev picks them (datasets.md) · NEVER written  │
   │ datasets marked IGNORED in datasets.md: never queried        │
   └──────────────────────────────────────────────────────────────┘


  EXAMPLE: two panes on the same repo
    pane A  "the header must reuse the footer grid"
            ③ Jev: this project → draft.md
    pane B  "why is the header misaligned?"
            ① Jev reads the same draft.md, ② finds the line
            → added, without asking Cognee

  "ask before committing" applies everywhere: it belongs in CLAUDE.md /
  AGENTS.md, which the agent already loads. memsift keeps project memory only.
```

</details>

## Why it is built this way

1. **A decision model, not a chat model, decides what to inject.** Jev returns
   probabilities, not text. It reads up to 26 000 tokens of context and answers
   in a fraction of a second, for a fraction of a cent. That is what makes it
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
├── datasets.md            what each Cognee dataset is for, one line each
├── debug.log              every Jev decision in plain text, when MEMSIFT_DEBUG=1
└── repos/<repo>/
    ├── digest.md          compacted project memory (≤ 3 000 tokens)
    ├── draft.md           turns kept by Jev since the last compaction
    ├── pending.jsonl      turns noted while Jev was unreachable
    └── cognee-index.json  what Cognee holds, refreshed each session

<repo>/jev.md              a short project card: what it is, what matters, which phase
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
- an [OpenRouter](https://openrouter.ai) key, for Jev
- any OpenAI-compatible endpoint for the compactor, for example Gemma 4 on
  [DeepInfra](https://deepinfra.com)

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
`/memory-doctor`: it checks the configuration and that Cognee, Jev and the
compactor answer, and says what to fix.

## Configuration

Settings are read from the environment, then from `~/.cognee/.env`. The names
below are the planned ones; [docs/SPEC.md](docs/SPEC.md), part 7, is the
reference.

| Variable | Default | Description |
|---|---|---|
| `COGNEE_BASE_URL` | none (required) | URL of your Cognee server |
| `COGNEE_API_KEY` | none (required) | Cognee API key, sent as `X-Api-Key` |
| `OPENROUTER_API_KEY` | none (required) | key for Jev; usage shows on your OpenRouter dashboard |
| `MEMSIFT_JEV_MODEL` | `typesafe/jev-1.13` | Jev model on OpenRouter |
| `MEMSIFT_COMPACTOR_URL` | DeepInfra's OpenAI-compatible endpoint | any OpenAI-compatible base URL |
| `MEMSIFT_COMPACTOR_API_KEY` | none (required) | key for that endpoint |
| `MEMSIFT_COMPACTOR_MODEL` | Gemma 4 26B-A4B | compactor model id at that endpoint |
| `MEMSIFT_DATASET` | `memsift` | the Cognee dataset memsift writes to; it never writes elsewhere |
| `MEMSIFT_DEBUG` | `0` | `1` writes every Jev decision to `~/.memsift/debug.log` |

## What leaves your machine

| Sent to | What | When |
|---|---|---|
| OpenRouter (Jev) | your prompt, the recent exchanges, the working notes, `jev.md` | each prompt and after each answer |
| the compactor | the working notes of one project | when they are full, and at session end |
| Cognee | the cleaned notes, the dataset list | at compaction; the list at session start |

Secrets are redacted before anything is sent: API keys, tokens, private keys,
and values of variables whose names look like credentials. Tool output is never
sent as is; memsift only keeps a short note of what changed.

## Cost

At the time of writing, Jev costs $0.042 per million input tokens. A prompt
with a full context (about 26 000 tokens) costs about a tenth of a cent; most
prompts carry much less. A compaction with Gemma 4 26B on DeepInfra
($0.07 in, $0.34 out per million tokens) costs well under a cent. Cognee runs
on your own server.

## Watching what it does

With `MEMSIFT_DEBUG=1`, each decision is written in plain text:

```
tail -f ~/.memsift/debug.log

  "you're annoying me"            → ephemeral → dropped
  "back to the header"            → missing 0.85 · in notes 0.91 → 1 line added
```

## When something fails

| Down | What happens |
|---|---|
| Jev | memsift pauses entirely; turns are queued in `pending.jsonl` and sorted on resume |
| the compactor | Jev keeps working; compaction waits |
| Cognee | Jev keeps working from the local notes; packets wait to be sent |

Three failures in a row trigger the pause. The agent is told once, in one line,
and you resume with `/memory-resume`. On a prompt, Jev gets two seconds; past
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
