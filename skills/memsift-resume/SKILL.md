---
name: memsift-resume
description: Use when the user explicitly requests memsift resume. Explicitly probe and resume memory services, drain queued packets and sort pending turns.
---

Resolve PLUGIN_ROOT as the absolute path two directories above the directory containing this SKILL.md (the plugin root). Run this entry once in the current repository and report its output to the user:

```bash
python3 "${PLUGIN_ROOT}/memsift/run.py" resume --host codex --cwd "$PWD"
```

Do not read or display credential values. Do not edit configuration or jev.md, or generate repairs.
