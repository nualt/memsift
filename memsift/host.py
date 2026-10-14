"""Host boundary.

normalize(event, payload, host_name) -> Event; render(event, context) -> str;
read_conversation(path, host_name, actions, ratio) -> Conversation;
fit_prompt(state, questions, ratio) -> dict; snapshot(root) -> str;
note_tool(root, session, tool) -> dict; detach_args() -> dict.
"""
