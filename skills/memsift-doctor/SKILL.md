---
name: memsift-doctor
description: Use when the user explicitly requests memsift doctor. Check memory configuration, services and local files without changing service pause state.
---

Resolve PLUGIN_ROOT as the absolute path two directories above the directory containing this SKILL.md (the plugin root). Run this entry once in the current repository and report its output to the user:

```bash
python3 "${PLUGIN_ROOT}/memsift/run.py" doctor --host codex --cwd "$PWD"
```

Do not read or display credential values. Do not edit configuration or jev.md, or generate repairs.
