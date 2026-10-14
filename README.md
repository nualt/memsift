# memsift

Memory for coding agents (Claude Code, Codex), backed by a Cognee server.
It gives the agent only what it is missing, and most of the time that is nothing.

Status: early fork, specification in progress, not usable yet.
See [docs/SPEC.md](docs/SPEC.md).

## The problem

Memory plugins for coding agents usually recall on every prompt: they query the
knowledge graph, take the closest passages and paste them into the context.
In practice that means:

- a few thousand characters added to every prompt, unsorted;
- passages chosen for being close to the words of the prompt, not for being useful;
- things the agent already has in front of it, pasted again;
- a wait of 6 to 12 seconds on every prompt, even "ok, go ahead".

## What memsift does

Before each prompt, a small decision model (Jev) answers three questions in
one request, in a fraction of a second:

1. Is the agent missing something that already exists somewhere?
2. Is it in this project's working notes, kept on disk?
3. If not, is it worth asking Cognee, and in which datasets?

```
  your prompt
      │
      ▼
  Jev: is anything missing?
      │
      ├── no (most prompts) ──────────────────────► nothing is added
      │
      ├── yes, in the working notes ──────────────► the useful lines are added
      │
      └── yes, worth asking Cognee
              │
              ▼
          Cognee returns its closest passages
              │
              ▼
          Jev keeps only those that change what the agent will do
              │
              ▼
          a few lines are added, often none
```

After each answer, Jev decides whether the turn taught anything worth keeping:

| Kind | Example | Where it goes |
|---|---|---|
| how you like to work | "ask before committing" | `preferences.md`, all projects, right away |
| this project | "the header uses the footer grid" | the project's working notes, then Cognee |
| a technical lesson | "pnpm blocks build scripts unless allowed" | Cognee only |
| nothing | "ok", typos, status updates | dropped |

What can already be read in the repo (code, docs, `CLAUDE.md`) is not stored again.
When the working notes fill up, a cheap model (Gemma) cleans them, sends them to
Cognee and rewrites a short digest of the project.

## Files on disk

```
~/.memsift/
├── preferences.md         how you work, shared by every project
├── datasets.md            what each Cognee dataset is for
└── repos/<repo>/
    ├── draft.md           working notes kept by Jev
    ├── digest.md          decisions and work in progress
    ├── pending.jsonl      turns noted while Jev was unreachable
    └── cognee-index.json  what Cognee holds, refreshed each session

<repo>/jev.md              a short project card, written with you
```

Every session on the machine reads the same files, so a preference learned in
one project applies in the next prompt of another.

## Failures

After three failures in a row, memsift pauses instead of retrying in the
background. The agent is told once, in one line, and you resume with
`/memory-resume`. If Cognee is down, Jev keeps working from the local notes.

## Hosts

| Host | Status |
|---|---|
| Claude Code | planned for 0.1 |
| Codex | planned for 0.1 |
| Gemini CLI, Cursor, others | later, through the same core |

## Requirements

- Python 3.9 or later, standard library only, nothing to install
- a running [Cognee](https://github.com/topoteretes/cognee) server
- an [OpenRouter](https://openrouter.ai) key, for Jev
- any OpenAI-compatible endpoint for the compactor (for example Gemma 4 on DeepInfra)

Configuration goes in `~/.cognee/.env`: `COGNEE_BASE_URL`, `COGNEE_API_KEY`,
`OPENROUTER_API_KEY`, and the compactor's endpoint, key and model. The full list
will be in [docs/SPEC.md](docs/SPEC.md), part 7.

## Origin and license

memsift is a fork of the Claude Code and Codex integrations from
[topoteretes/cognee-integrations](https://github.com/topoteretes/cognee-integrations)
(Claude Code plugin 1.6.6, Codex plugin 1.7.5), with their git history.
It has been modified substantially. It is not affiliated with or endorsed by Cognee.

Licensed under Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
