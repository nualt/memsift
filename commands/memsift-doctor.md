---
description: Check memory configuration, services and local files without changing service pause state.
---

Run this entry once in the current repository and report its output to the user:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/memsift/run.py" doctor --host claude-code --cwd "$PWD"
```

Do not read or display credential values. Do not edit configuration or jev.md, or generate repairs.
