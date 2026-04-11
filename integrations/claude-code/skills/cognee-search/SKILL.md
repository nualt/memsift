---
name: cognee-search
description: Search Cognee memory. Session memory is automatically searched on every prompt via hooks. Use this skill explicitly for permanent knowledge graph search, filtered category search, or when you need more results than the automatic lookup provides.
---

# Cognee Memory Search

Search both session memory and the permanent knowledge graph, optionally filtered by data category.

## Automatic session search

Session memory is searched **automatically on every user prompt** via the `UserPromptSubmit` hook. You do not need to run this skill to access current-session context.

## Data categories

Knowledge is organized into three categories via `node_set`:

| Category | Node set | Contains |
|----------|----------|----------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts |

## Instructions

### Search session memory (current session, more results)

```bash
cognee-cli recall "$ARGUMENTS" -s "${COGNEE_SESSION_ID:-claude_code_session}" -k 10 -f json
```

### Search permanent graph (all categories)

```bash
cognee-cli recall "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" -k 5 -f json
```

### Search permanent graph (specific category)

```bash
# User data only
cognee-cli recall "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set user_context -k 5 -f json

# Project data only
cognee-cli recall "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set project_docs -k 5 -f json

# Agent data only
cognee-cli recall "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set agent_actions -k 5 -f json
```

### Search both (session first, fallback to graph)

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS"
```

## Understanding results

Results include a `_source` field:
- `"session"` — from the session cache (current conversation)
- `"graph"` — from the permanent knowledge graph

Session entries tagged with `[category:agent]` are automatic tool call logs.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | Already automatic, no action needed |
| "what are my preferences" | Search graph with `--node-set user_context` |
| "what does the codebase do" | Search graph with `--node-set project_docs` |
| "what did we do last time" | Search graph (all categories) with `-d` |
| User explicitly says "search cognee" | Search graph with `-d` |
| Auto context insufficient | Search session with `-s -k 10` |
