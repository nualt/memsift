---
description: Explicitly probe and resume memory services, drain queued packets and sort pending turns.
---

Run this entry once in the current repository and report its output to the user:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/memsift/run.py" resume --host claude-code --cwd "$PWD"
```

Do not read or display credential values. Do not edit configuration or jev.md, or generate repairs.
