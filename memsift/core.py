"""Event coordination.

prepare(event) -> Work; dispatch(work, config) -> str; start, prompt, tool,
stop and end(work, config) -> str; resume(root, config, work=None) -> str;
replay_pending(work, config) -> ReplayResult.
"""
