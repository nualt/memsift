---
name: cognee-remember
description: Store data permanently in the Cognee knowledge graph. Accepts a data category (user, project, or agent) to tag the data with the correct node_set for filtered retrieval.
---

# Cognee Permanent Memory Storage

Store data permanently in the Cognee knowledge graph with category tagging.

## Data categories

Cognee organizes knowledge into three categories via `node_set` tagging:

| Category | Node set | What belongs here |
|----------|----------|-------------------|
| **user** | `user_context` | User preferences, corrections, personal facts, communication style |
| **project** | `project_docs` | Repository docs, code context, architecture decisions, company data |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts (auto-captured by hooks) |

## Instructions

Determine the category from the user's intent, then run:

**User data** (preferences, corrections, personal context):
```bash
cognee-cli remember "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set user_context
```

**Project data** (docs, code, company knowledge):
```bash
cognee-cli remember "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set project_docs
```

**Agent data** (explicit agent notes — routine tool logs are automatic):
```bash
cognee-cli remember "$ARGUMENTS" -d "${COGNEE_PLUGIN_DATASET:-claude_sessions}" --node-set agent_actions
```

If the category is unclear, default to **project**.

The command outputs a summary after completion:

```
Data ingested and knowledge graph built successfully!
  Dataset ID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
  Items processed: 1
  Content hash: a1b2c3d4...
  Elapsed: 4.2s
```

**IMPORTANT**: Do NOT use the `-b` (background) flag. Always run in the foreground to ensure the full pipeline completes.

## When to use

- User says "remember this" or "save this" → category **user**
- User says "remember this about the project/codebase" → category **project**
- You want to persist your own findings or conclusions → category **agent**
- NOT for routine tool call logging (that's automatic via hooks with `agent_actions` tagging)

## Category routing guide

| Signal | Category |
|--------|----------|
| "remember my preference for..." | user |
| "I always want..." / "I prefer..." | user |
| "remember this about the codebase" | project |
| "save these docs" / "index this file" | project |
| "note that this API works like..." | project |
| "remember what we discovered" | agent |
| Routine tool calls | agent (automatic, no action needed) |
