# memsift

Memory for coding agents (Claude Code, Codex), backed by a Cognee server.

Instead of injecting thousands of characters of recalled memory into every prompt,
memsift asks a small decision model whether the agent is actually missing something
that already exists, and injects only that. Most prompts get nothing.

Status: early fork, specification in progress. Not usable yet.

## Origin and license

memsift is a fork of the Claude Code and Codex integrations from
[topoteretes/cognee-integrations](https://github.com/topoteretes/cognee-integrations)
(Claude Code plugin 1.6.6, Codex plugin 1.7.5), with their git history.
It has been modified substantially. It is not affiliated with or endorsed by Cognee.

Licensed under Apache-2.0, see [LICENSE](LICENSE).
