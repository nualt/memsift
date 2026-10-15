# memsift architecture

How memsift fits together, and why it is built this way. The exact behaviour
(files, questions, thresholds, tests) is in [SPEC.md](SPEC.md); this document
gives the picture and the reasons, and does not repeat the numbers it does not
need.

Contents:

1. Overview
2. The full picture
3. Repo map
4. Key choices

## 1. Overview

```
                      YOUR MACHINE                                    REMOTE
  ┌──────────────┐   hook JSON   ┌───────────┐  state + questions  ┌──────────────────────┐
  │ Claude Code  │ ────────────► │           │ ──────────────────► │ Jev (OpenRouter)     │
  │ Codex        │ ◄──────────── │           │ ◄────────────────── │ sorts, never writes  │
  └──────────────┘ added context │           │    probabilities    └──────────────────────┘
                                 │           │
                                 │  memsift  │  working notes      ┌──────────────────────┐
                                 │           │ ──────────────────► │ compactor (Haiku)    │
                                 │           │ ◄────────────────── │ the only writer      │
                                 │           │  digest + packet    └──────────────────────┘
                                 │           │
                                 │           │  packets, recall    ┌──────────────────────┐
                                 │           │ ◄─────────────────► │ Cognee server        │
                                 └─────┬─────┘                     │ long-term store      │
                                       │                           └──────────────────────┘
                                       ▼
                          ~/.memsift/      working notes and state, per repo
                          <repo>/jev.md    the project card, owned by the user
```

Three places can hold what the agent is missing, from cheapest to most
expensive:

```
what the agent already sees      free        nothing to add
the repo's working notes         local, ms   digest.md + draft.md, read by Jev
Cognee                           ~6 s        asked only when Jev judges it worth it
```

Two models, two jobs. **Jev** is a classifier: it reads a lot of context and
returns probabilities, and code decides with thresholds. It decides what to
add to the agent's context and what to keep from each turn. **The compactor**
(Claude Haiku by default) is the only component that writes sentences: from time
to time it turns the raw notes into a short digest and a clean packet for
Cognee.

## 2. The full picture

A session, moment by moment. Probabilities and thresholds are starting points,
set at calibration (SPEC part 4).

```
  WHAT JEV READS  (cheap for Jev, never pasted as is into the agent)
  ┌────────────────┐ ┌────────────────┐ ┌──────────────────────┐ ┌────────────────┐
  │ jev.md         │ │ CLAUDE.md,     │ │ the conversation     │ │ the repo's     │
  │ project, what  │ │ AGENTS.md      │ │ since the last       │ │ WORKING NOTES  │
  │ matters, phase │ │ "already       │ │ /compact, its end    │ │ digest.md +    │
  │ (③ only)       │ │ written" (③)   │ │ (≤ 25 000 tokens)    │ │ draft.md       │
  └────────────────┘ └────────────────┘ └──────────────────────┘ └────────────────┘


 ⓪ SESSION START ───────────────────────────────────────── hook SessionStart
     │   returns nothing
     ├─ session file: repo, host, how the session started
     │  (new, /clear, /compact, resume)
     ├─ no jev.md at the repo root?  ──► flag "propose a first version"
     └─ in the background:
          Cognee: list of what it holds ──► cognee-index.json
          a dataset with no line in datasets.md? ──► flag "propose a description"
          packets waiting in the outbox ──► sent


 ① YOU TYPE A PROMPT ──────────────────────────────── hook UserPromptSubmit
     │   the only hook you wait for
     ▼
   ┌──────────── JEV · questions sent at once · 2 s at most ──────────┐
   │ ① "What kind of prompt is it?" eleven kinds; five need nothing   │
   │     (reaction, self-contained task, general question…)           │
   │     reads: the prompt + the end of the conversation              │
   │ ② for each notes entry, ONE per question:                        │
   │     "Does this entry answer the prompt?"                         │
   │     reads: the prompt + that entry only                          │
   │ ③ "Is the topic the kind kept?" (choice, constraint, problem met │
   │    before) → worth asking COGNEE; and, for each dataset:         │
   │    "Would it hold the answer?" (datasets.md + cognee-index.json) │
   │ → each answer is a probability (0 to 1); code decides            │
   └────────────────────────────────┬─────────────────────────────────┘
          ┌─────────────────────────┴─────────────────────────┐
          ▼                         ▼                         ▼
   ① needs nothing ≥ 0.5     entries with ② ≥ 0.5      no such entry, ③ ≥ 0.8
   (most prompts)            → those entries           (otherwise: nothing)
   → NOTHING ADDED             are added as they are             │
                               │                                 ▼
                               │                     COGNEE, on the datasets
                               │                     Jev picked (12 s at most):
                               │                     its 5 passages CLOSEST to
                               │                     the prompt, not the most
                               │                     useful ones
                               │                                 │
                               │                                 ▼
                               │         ┌──────────── JEV ④ · 2 s at most ────────────┐
                               │         │ for each passage, ONE per question:         │
                               │         │   "Does it answer the prompt?"   ≥ 0.7      │
                               │         │   → kept; otherwise dropped                 │
                               │         └────────────────────┬────────────────────────┘
                               │                              │
                               └──────────────────────────────┤
                                                              ▼
   ┌────────────────────────────── AGENT CONTEXT ───────────────────────────────┐
   │ … the conversation …                                                       │
   │ [ what the sorting kept, often nothing. For example:                       │
   │   "Decision: the header reuses the footer grid" ]                          │
   │ [ pending flags: "This repo has no jev.md. Propose a first version." ]     │
   │ your prompt                                                                │
   └────────────────────────────────────────────────────────────────────────────┘

   A step that times out ends the hook with nothing from memory: unsorted
   Cognee passages are never pasted.

   WAIT   usual plugin: ~6 s on EVERY prompt (median, up to 12 s)
          memsift     : Jev alone on most prompts, under 2 s;
                        2 + 12 + 2 = 16 s at worst, only when Cognee is asked


 ② THE AGENT WORKS ─────────────────────── hooks PostToolUse, PostToolUseFailure
     │   nothing is sent anywhere
     ▼
   ┌─────────────── ACTION NOTE · local, plain code ───────────────┐
   │ Edit / Write in the repo            ✔ noted (short diff)      │
   │ Edit / Write in /tmp, scratch dirs  ✗ ignored                 │
   │ Bash that changed the repo          ✔ noted                   │
   │ Bash that changed nothing           ✗ ignored                 │
   │ failed, then succeeded              ✔ marked "lesson?"        │
   │ reads, searches, web                ✗ ignored                 │
   └───────────────────────────────────────────────────────────────┘
   "Changed the repo" = the hash of HEAD + the uncommitted diff moved.
   The notes of the last 5 turns stay in the session file.


 ③ THE AGENT ANSWERED ──────────────────────── hook Stop · in the background
     │   your prompt + its answer + the action notes
     ▼
   ┌────────────────────────────── JEV ───────────────────────────────┐
   │ "What kind of information is this?"                              │
   │    this project · technical lesson · nothing                     │
   │ "A working habit for every project?"                             │
   │ "How long does it stay true?"  turn · task · project · always    │
   │ "Is the user correcting the agent?"                              │
   │ "Does this turn confirm a pending correction?"                   │
   │ "Already known?"  notes, CLAUDE.md, AGENTS.md, documented files  │
   │ "Is jev.md outdated?"                                            │
   │ "A structuring decision without its reason?"                     │
   └────────────────────────────────┬─────────────────────────────────┘
     ┌──────────────────────────────┼──────────────────────────────┐
     ▼                              ▼                              ▼
   DROPPED                     KEPT → draft.md                FLAGS, added to
   "commit pls"                "we go with Stripe:             the next prompt
   "ask before committing"       PayPal fees too high"        jev.md outdated
     (every project: belongs   "no Tailwind in this repo"     → the agent proposes
     in CLAUDE.md, AGENTS.md)  "pnpm blocks build scripts"      an update
   already known               raw turn + labels, since       decision without
   seen in the code            Jev cannot write               its reason
                               a correction is marked         → the agent asks why
                               "pending" until a later
                               turn confirms it
                                  │
                                  ▼
                    notes full?  (digest + draft ≥ 8 000 tokens)
                    no → carry on
                                  │ yes
                                  ▼
   ┌───────────────── COMPACTION · in the background ─────────────────┐
   │ one at a time per repo (lock); a second one does nothing         │
   │ HAIKU reads ALL of the repo's notes, in one request:             │
   │   ├─ packet: what is new since the last compaction, cleaned      │
   │   │    and merged, lessons included ──────► COGNEE (or outbox)   │
   │   └─ digest.md updated: Haiku names old lines to replace or      │
   │        drop, and adds every new project entry; code keeps the    │
   │        other lines word for word · in progress · decisions ·     │
   │        project facts · working rules; never lessons; an option   │
   │        only considered is kept, marked as considered             │
   │        lines numbered by code; at 3 000 tokens, emptied but for  │
   │        the working rules                                         │
   │ draft.md emptied, except lines added meanwhile and corrections   │
   │ still pending                                                    │
   │ invalid answer twice → nothing written, the notes stay as is     │
   │                                                                  │
   │ meanwhile, Jev keeps using the old notes                         │
   └──────────────────────────────────────────────────────────────────┘

   What drops out of digest.md is not lost: it is already in Cognee.


 ④ SESSION END ─────────────────────────────── hook SessionEnd · in the background
     │   the terminal closes at once
     ├─ draft.md not empty → compaction, even below 8 000 tokens
     └─ session file deleted
   A session that crashes skips this step: its entries stay in draft.md
   and go out with the next compaction.


 WHEN A SERVICE FAILS ───────────────────────────────────── any moment
   timeout, no answer, 5xx     counts 1; at 3 in a row the service is paused
   key refused (401, 403)      paused at once
   rate limited (429, 529)     not counted; this turn goes without it
   ┌─────────────┬───────────────────────────────────────────────────┐
   │ Jev         │ everything pauses; turns wait in pending.jsonl    │
   │ compactor   │ Jev keeps working; compaction waits               │
   │ Cognee      │ Jev keeps working on the local notes; packets     │
   │             │ wait in the outbox                                │
   └─────────────┴───────────────────────────────────────────────────┘
   The agent tells you once, in one line. /memsift-resume restarts what
   answers again and catches up.

═══════════════════════════════════════════════════════════════ COGNEE SERVER ═══

   ┌──────────────────── COGNEE · memsift dataset ────────────────────┐
   │ receives only clean, deduplicated packets, named                 │
   │ "<repo> <date> <time> <topics>" so Jev can read the list         │
   │ stores and links them (graph)                                    │
   │ → asked only at ①, when Jev judges it worth it                   │
   └──────────────────────────────────────────────────────────────────┘

   ┌─────────────────── YOUR CURATED KNOWLEDGE ───────────────────┐
   │ your other datasets: playbooks, projects, lessons…           │
   │ read at ① when Jev picks them (datasets.md) · NEVER written  │
   │ datasets marked IGNORED in datasets.md: never asked          │
   └──────────────────────────────────────────────────────────────┘


  EXAMPLE: two panes on the same repo
    pane A  "the header must reuse the footer grid"
            ③ Jev: decision, this task → draft.md
    pane B  "why is the header misaligned?"
            ① Jev reads the same draft.md, ② finds the entry
            → added, without asking Cognee
```

## 3. Repo map

The repo root is the plugin root for both hosts. Who calls whom:

```
hooks/claude-code.json ──┐
hooks/codex.json ────────┴──► memsift/run.py   reads the hook JSON, never fails the host
                                    │          host.py: every difference between hosts
                                    ▼
                                 core.py        start · prompt · tool · stop · end
            ┌──────────────┬────────┴─────┬──────────────┬──────────────┐
            ▼              ▼              ▼              ▼              ▼
        store.py        jev.py      compactor.py    cognee.py      breaker.py
        files on disk,  questions,  model request,  recall, add,   failures,
        locks           thresholds  answer checks   dataset list   pauses

        shared: vendor/, kept from upstream (Cognee HTTP, redaction,
                ~/.cognee/.env, file lock, bounded logs)

commands/memsift-resume ──► tests paused services, sends the outbox,
                            sorts pending.jsonl
commands/memsift-doctor ──► doctor.py: configuration, keys, services reachable
```

`tests/` runs offline against a local server that plays Jev, the compactor
and Cognee. The file-by-file tree is in SPEC part 1.

## 4. Key choices

### A classifier sorts, one model writes

Deciding whether the agent is missing something, or whether a passage is
useful, is classification. Jev does only that: it gets a state and questions,
and returns probabilities. It cannot write a sentence.

The asymmetry is what makes it work. The agent's context is precious; Jev's is
almost free (32 000 tokens, $0.042 per million). memsift can give Jev a lot of
context on every prompt and still add nothing to the agent.

Since Jev cannot write, everything it picks is copied as it is: a notes entry
is the raw turn with labels added by code, and what reaches the agent are
entries as written. Only the compactor rewrites text. It needs fidelity: keep
the user's words, invent nothing, and settle corrections right. Speed does not
matter, since it runs in the background, so a model that reasons before
answering is affordable; on a first test it was the only one that kept every
statement right.

### Small questions, checked on real turns

Jev does best with one plain judgment per question. The first design asked it to
sort each turn into five kinds: working rule, decision, project fact, lesson,
nothing. A test on a hundred prompts typed the way a real user types (short,
casual, misspelled) showed Jev splitting its answer between the first three:
"no Tailwind in this project" came out as a working rule at 0.46 and a project
fact at 0.29, and the turn was dropped for that hesitation, although all three
kinds lead to the same place. The kinds were cut to three that do not overlap:
this project, technical lesson, nothing. On the same turns, "no Tailwind" moved
to 0.99. Telling a general habit ("ask before committing", which belongs in
`CLAUDE.md`) from a rule of this project is now a separate yes/no question,
instead of being read from a scope of "always" that also covered permanent
project facts ("prices are shown with VAT").

The same test showed the prompt questions had the same flaw. They sent Jev one
large state (the conversation, the compaction summary, `jev.md`, every entry,
the datasets) and asked whether the agent "lacks information that already
exists and is not in the conversation": no entry was pasted when ten were
needed, and "commit" scored 0.77 on an unrelated entry. They now ask three
plain things, each with only what it needs: does the conversation already hold
what the agent needs (prompt and conversation), does this entry answer the
prompt (prompt and one entry), and, for Cognee, does this passage answer the
prompt (prompt and one passage). On the same prompts, five of six recalls were
pasted, almost all noise got nothing, and right Cognee passages scored 0.88 to
0.92 against at most 0.63 for wrong ones.

The compaction summary no longer counts as something the agent has: it keeps
the gist, not the detail. And Jev reads only the end of the conversation, up to
25 000 tokens: what lies further back, the agent uses poorly anyway.

The lesson for the rest of the design: a question to Jev is written, then run on
real turns, before it is relied on.

Two of those three questions changed again after a test on a held-out project
and 47 labelled real prompts. Asking whether the conversation already holds
what the agent needs, Jev took "the conversation discusses the topic" for "it
states the fact": a prompt about a database set up weeks before was stopped
because the conversation had just explained how such databases work. Neither a
sharper wording nor a "topic only" choice moved it. Sorting the prompt by kind
did: Jev is good at telling what kind of thing it reads, not at telling what
someone else knows. The same held for Cognee: "is it worth searching?" never
reached its threshold, and the six prompts that needed Cognee were scattered
between ranks 2 and 31 out of 47. Asking whether the topic is the kind memsift
keeps put three of them first; Cognee only holds what the end-of-turn sorting
kept, so a topic it drops is not worth a search.

The compactor had a gap of its own: its instructions said what goes into the
packet but never that new project entries also go into the digest, and it
often left them in the packet only. On three invented projects, 13 of 18 new
entries reached the digest; with one sentence added, 18 of 18.

### Only what is missing, and already exists

The test is not "is it in the agent's context?" but "is it in front of it,
now?". A model makes poor use of what lies far back in a long context: a
decision from 80 messages ago, or lost in a `/compact` summary, may come back;
one from two prompts ago may not. And memory is added only if it changes what
the agent does next: related but useless context is not neutral, it dilutes
attention.

Missing also means it already exists somewhere. While writing a spec,
everything is new: asking Cognee then is waiting for nothing. Three weeks
later, asking why a choice was made is exactly when memory helps.

### Cheapest first, and wait for Cognee only when it is worth it

What the agent sees, then the local notes, then Cognee. Most prompts stop at
Jev. When Jev judges Cognee worth it, the prompt waits for it (12 s at most,
about 6 s usually). Searching in the background instead would let the prompt
go at once, but the agent would start without the information and receive it
mid-work, or at the next prompt, too late. Losing a few seconds sometimes is
better than losing work.

Cognee returns the passages closest to the words, not the most useful ones,
so Jev sorts them before anything is pasted. The local notes need no second
pass: Jev picks their entries by reading them.

### The repo is the memory of what is in it

Code, docs and instruction files already hold the details. Memory keeps what
cannot be read in the repo: why a choice was made, what was ruled out, where
the work stands. The test for keeping a turn is whether it would save redoing
the whole path if the same situation came back tomorrow: a technical choice and
its reason, a constraint that causes problems, a problem met and what fixed
it. "The button is blue" or "a 12-column grid" belong in a design system; "we
chose Medusa because…" belongs in memory. While a spec is being written, its content ends up in the
spec file: the action note shows the edit, Jev sees it as already known, and
nothing is stored twice.

For the same reason memsift keeps project memory only. General working rules
("ask before committing") belong in `CLAUDE.md` or `AGENTS.md`, which the user
owns and the agent loads in full. memsift keeps no second copy: one source of
truth per thing.

Each level holds what matches its weight:

```
jev.md       what is structural; changes rarely; the user approves every change
digest.md    decisions and facts of the project, capped; small ones fade out
Cognee       everything, over time
```

### Raw turns in, compacted in batches

Every turn worth keeping is appended to `draft.md` as it is. Compacting once
the notes are full, rather than at each turn, is cheaper (one request every few
dozen turns) and better: seeing the whole batch, the compactor merges
duplicates and settles contradictions, the most recent winning.

Code keeps control of what the compactor cannot do reliably: it numbers the
digest lines (so Jev can point at them), cuts a digest that grows too long
(models count tokens poorly, and a growing digest would end in a compaction
at every turn), and rejects an answer that does not match the schema. A
compactor that goes wrong never damages the notes. The packet holds only what
is new, so Cognee never receives the same thing twice.

### Corrections wait for proof

A "no" alone is not enough: the user may change their mind two messages
later. A correction gets in more easily than another turn, but it stays
pending until later turns confirm it (an edit, a commit, no going back). Then
a line `- [d4] confirmed` is appended, and the compactor replaces the old
statement. A change of mind is simply a new correction.

### Shared files, append-only

Several sessions often run on the same repo: two panes, or Claude Code and
Codex side by side. Every hook reads the files again, so a decision noted in
one pane is seen at the next prompt of the other. Entries are only ever
appended, in one write of whole lines; rewrites go through a temporary file
and a rename, under a lock. No line is ever edited in place, which is what
lets several writers share a file safely. A repo's key includes a hash of its
path, so two clones with the same name never mix their notes.

### Only the prompt hook makes you wait

Each hook is a separate process. Slow work (sorting a turn, compacting,
rebuilding the Cognee index) runs in a detached process that outlives the hook
and the session, so closing the agent never waits for memsift. There is no
`PreCompact` hook: every turn worth keeping is already in `draft.md` when the
stop hook ends.

### One codebase, two hosts

Claude Code and Codex share the same hook events and nearly the same JSON.
One `host.py` holds every difference; the rest of the code does not know
which host it runs in. A third host adds branches there and one hooks file.
Codex encrypts its compaction summaries, so for Codex Jev reads the user
messages it keeps in clear instead: it knows what was discussed, if not all
the agent retained, and may sometimes paste an entry the agent already has.

### Stop instead of failing quietly

Memory must never block work, and no model should run for nothing. A hook
never fails the host: every error is caught. The other side of that is that
an error could go unnoticed, so every error is always written to
`~/.memsift/debug.log` with its traceback. A service that fails three times in
a row is paused for every session on the machine, and the agent tells the user
in one line, the one channel that works on every host without installing
anything. A refused key pauses at once, since retrying cannot help; rate
limits do not count, so a provider's busy minute does not stop memsift.
Nothing is lost while paused: turns and packets wait on disk.

### Nothing to install, little to configure

Python 3.9 or later, standard library only: a hook runs `python3 run.py` with
the system interpreter, with no virtualenv. This also ruled out frameworks
such as LangGraph for session state: each hook is a separate process, so state
has to be on disk anyway. Settings are limited to addresses, keys, models, the
dataset and debug mode. Thresholds, sizes and timeouts live in the code and
change only with calibration: fewer settings, fewer ways to break.

### Secrets never leave, raw output never leaves

Every text is redacted before it is written and before it is sent, so no file
under `~/.memsift/` and no request holds a key. Tool output is never sent:
the fact that something changed is a signal, its content is noise. memsift
writes only to its own Cognee dataset, never to one another tool writes to.
