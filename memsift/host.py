"""Host normalization, bounded transcript tails and local action notes."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from . import store
from .vendor.logfiles import decision, error
from .vendor.redact import redact

_EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "PostToolUse": "tool",
           "PostToolUseFailure": "tool", "Stop": "stop", "SessionEnd": "end"}
_PENDING = " (failed pending)"
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _reverse_lines(path):
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        carry = b""
        while position:
            size = min(65536, position)
            position -= size
            stream.seek(position)
            pieces = (stream.read(size) + carry).split(b"\n")
            carry = pieces[0]
            for line in reversed(pieces[1:]):
                if line.strip():
                    yield line
        if carry.strip():
            yield carry


def _records(path):
    path = Path(path)
    if not path.is_file():
        return
    try:
        for line in _reverse_lines(path):
            try:
                row = json.loads(line.decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("transcript record must be an object")
                yield row
            except (UnicodeError, ValueError) as exc:
                error("host-transcript-record", exc)
    except OSError as exc:
        error("host-transcript-read", exc)


def _text(content, types):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") in types
                     and isinstance(block.get("text"), str))


def _message(row, host_name):
    if host_name == "claude-code":
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain"):
            return None
        message = row.get("message", {})
        role = row["type"]
        types = ("text",)
    else:
        if row.get("type") != "response_item":
            return None
        message = row.get("payload", {})
        if not isinstance(message, dict) or message.get("type") != "message":
            return None
        role = message.get("role")
        types = ("input_text",) if role == "user" else ("output_text",)
    if not isinstance(message, dict) or role not in ("user", "assistant"):
        return None
    text = redact(_text(message.get("content"), types))
    return {"role": role, "text": text, "actions": []} if text else None


def read_conversation(path: Path, host_name: str, actions: list, ratio: float) -> dict:
    summary = ""
    newest = []
    characters = 0
    full = False
    for row in _records(path):
        if host_name == "claude-code":
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                break
            if row.get("isCompactSummary"):
                message = _message(row, host_name)
                summary = message["text"] if message else ""
                # The boundary immediately precedes this summary in the transcript.
                break
        elif row.get("type") == "compacted":
            payload = row.get("payload", {})
            history = payload.get("replacement_history", []) if isinstance(payload, dict) else []
            clear = []
            for item in history:
                if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
                    text = _text(item.get("content"), ("input_text",))
                    if text:
                        clear.append(text)
            summary = redact("\n".join(clear))
            break
        message = _message(row, host_name)
        if full:
            if message and message["role"] == "user" and newest and newest[-1]["role"] == "assistant":
                message["text"] = store.clip(message["text"], 5000, ratio)
                newest.append(message)
            continue
        if message:
            cost = len(json.dumps(message, ensure_ascii=False))
            available = max(0, int(5000 * ratio) - characters)
            if cost > available:
                if not newest or (message["role"] == "user" and newest[-1]["role"] == "assistant"):
                    message["text"] = store.clip(message["text"], 5000, ratio)
                    newest.append(message)
                full = True
            else:
                newest.append(message)
                characters += cost
    exchanges = list(reversed(newest))
    matched = set()
    # shortcut: repeated prompt excerpts can be ambiguous, use transcript IDs if hosts later expose them.
    for group in reversed(actions):
        excerpt = redact(group.get("prompt", ""))[:200]
        if not excerpt:
            continue
        for index in range(len(exchanges) - 1, -1, -1):
            exchange = exchanges[index]
            if index not in matched and exchange["role"] == "user" and exchange["text"].startswith(excerpt):
                exchange["actions"] = [redact(item) for item in group.get("items", [])
                                       if not item.endswith(_PENDING)]
                matched.add(index)
                break
    # Keep complete user/assistant pairs when trimming the selected tail.
    while exchanges and exchanges[0]["role"] == "assistant":
        exchanges.pop(0)
    while store.estimate(_serialized(exchanges), ratio) > 5000 and exchanges:
        next_user = next((i for i in range(1, len(exchanges)) if exchanges[i]["role"] == "user"), None)
        if next_user is not None:
            exchanges = exchanges[next_user:]
            continue
        exchange = max(exchanges, key=lambda item: len(item["text"]))
        text = exchange["text"]
        if not text:
            exchanges = []
            break
        low, high = 0, len(text) - 1
        while low < high:
            middle = (low + high + 1) // 2
            exchange["text"] = store.clip(text, middle, 1.0)
            if store.estimate(_serialized(exchanges), ratio) <= 5000:
                low = middle
            else:
                high = middle - 1
        exchange["text"] = store.clip(text, low, 1.0)
    return {"summary": summary[:max(0, int(6000 * ratio))], "exchanges": exchanges}


def _command_exit(payload):
    use_id = payload.get("tool_use_id")
    path = payload.get("transcript_path")
    if not use_id or not path:
        return None
    for row in _records(Path(path)):
        record = row.get("payload", {})
        if row.get("type") != "event_msg" or not isinstance(record, dict):
            continue
        item = record.get("item", {})
        if (record.get("type") == "item_completed" and isinstance(item, dict)
                and item.get("type") == "CommandExecution" and item.get("id") == use_id):
            code = item.get("exit_code")
            return code if type(code) is int else None
    return None


def _patch_changes(command):
    changes = []
    for line in command.splitlines():
        match = re.match(r"\*\*\* (Update|Add|Delete) File: (.+)", line)
        if match:
            operation, path = match.groups()
            changes.append({"path": path, "kind": "write" if operation == "Add" else "edit", "diff": ""})
        elif line.startswith("*** Move to: ") and changes:
            changes.append({"path": line[len("*** Move to: "):], "kind": "edit", "diff": ""})
        elif changes and line.startswith(("+", "-")):
            changes[-1]["diff"] += line + "\n"
    return changes


def normalize(event: str, payload: dict, host_name: str) -> dict:
    if host_name not in ("claude-code", "codex") or event not in _EVENTS:
        raise ValueError("unsupported host or event")
    name = payload.get("tool_name", "")
    inputs = payload.get("tool_input", {})
    response = payload.get("tool_response", {})
    changes = []
    code = None
    failed = event == "PostToolUseFailure"
    if name == "Bash":
        if host_name == "codex":
            code = _command_exit(payload)
        elif failed:
            match = re.match(r"Exit code (-?\d+)\b", payload.get("error", ""))
            code = int(match[1]) if match else None
    elif not failed:
        if name == "apply_patch":
            changes = _patch_changes(inputs.get("command", ""))
        elif name in ("Edit", "MultiEdit", "NotebookEdit", "Write"):
            path = inputs.get("notebook_path" if name == "NotebookEdit" else "file_path", "")
            diff = ""
            if name == "Edit":
                diff = inputs.get("old_string", "") + " → " + inputs.get("new_string", "")
            elif name == "MultiEdit":
                diff = " · ".join(edit.get("old_string", "") + " → " + edit.get("new_string", "")
                                  for edit in inputs.get("edits", []))
            elif name == "NotebookEdit":
                old = response.get("old_source", "") if isinstance(response, dict) else ""
                diff = old + " → " + inputs.get("new_source", "")
            if path:
                changes = [{"path": path, "kind": "write" if name == "Write" else "edit", "diff": diff}]
    tool = {"name": name, "changes": redact(changes),
            "command": redact(inputs.get("command", "")) if name == "Bash" else "", "exit_code": code}
    source = payload.get("source", "")
    return {"event": _EVENTS[event], "host": host_name,
            "session_id": payload.get("session_id", ""), "cwd": payload.get("cwd", ""),
            "source": "resume" if source == "fork" else source,
            "transcript_path": payload.get("transcript_path", ""),
            "prompt": redact(payload.get("prompt", "")),
            "answer": redact(payload.get("last_assistant_message", "")), "tool": tool}


def render(event: dict, context: str) -> str:
    if event["event"] != "prompt" or not context:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": redact(context)}}, ensure_ascii=False)


def _serialized(value):
    return json.dumps(redact(value), ensure_ascii=False)


def _fits(state, questions, ratio):
    encoded = _serialized(state)
    longest = max((len(_serialized(q)) for q in questions.values()), default=0)
    request = {"model": "typesafe/jev-1.13", "state": state, "questions": questions}
    return (store.estimate(encoded + " " * longest, ratio) <= 32000
            and store.estimate(_serialized(request), ratio) <= 64000)


def fit_prompt(state: dict, questions: dict, ratio: float) -> dict:
    fitted = redact(copy.deepcopy(state))
    recent = fitted.get("recent_exchanges", [])
    if isinstance(recent, str):
        recent = re.split(r"(?=^User:)", recent, flags=re.MULTILINE)
        recent = [part for part in recent if part]
    else:
        recent = list(recent)
    while recent and not _fits(fitted, questions, ratio):
        recent.pop(0)
        if recent and isinstance(recent[0], dict):
            while recent and recent[0].get("role") == "assistant":
                recent.pop(0)
        fitted["recent_exchanges"] = "".join(recent) if isinstance(state.get("recent_exchanges"), str) else recent
    summary = fitted.get("compaction_summary", "")
    if summary and not _fits(fitted, questions, ratio):
        low, high = 0, len(summary)
        while low < high:
            middle = (low + high + 1) // 2
            fitted["compaction_summary"] = summary[:middle]
            if _fits(fitted, questions, ratio):
                low = middle
            else:
                high = middle - 1
        fitted["compaction_summary"] = summary[:low]
    notes = fitted.get("notes", {})
    for entry_id in list(notes):
        if _fits(fitted, questions, ratio):
            break
        if re.fullmatch(r"d\d+", entry_id):
            del notes[entry_id]
    if not _fits(fitted, questions, ratio):
        try:
            raise ValueError("Jev prompt exceeds the request budget with protected fields intact")
        except ValueError as exc:
            error("host-prompt-size", exc)
            raise
    return fitted


def snapshot(root: Path) -> str:
    started = time.monotonic()
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=0.25)
        head = git("rev-parse", "HEAD")
        if head.returncode:
            if b"not a git repository" in head.stderr or b"cannot change to" in head.stderr:
                return ""
            # rev-parse fails normally before the first commit.
            diff = git("diff", "--binary", _EMPTY_TREE)
            head_bytes = b"unborn HEAD"
        else:
            diff = git("diff", "HEAD", "--binary")
            head_bytes = head.stdout
        untracked = git("ls-files", "--others", "--exclude-standard")
        for result in (diff, untracked):
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, result.args,
                                                    output=result.stdout, stderr=result.stderr)
        return hashlib.sha256(b"\0".join((head_bytes, diff.stdout, untracked.stdout))).hexdigest()
    except (OSError, subprocess.SubprocessError) as exc:
        error("host-git-snapshot", exc, duration=time.monotonic() - started)
        return ""
    finally:
        decision("host-snapshot", {"duration": time.monotonic() - started})


def note_tool(root: Path, session: dict, tool: dict) -> dict:
    result = redact(copy.deepcopy(session))
    name = tool.get("name")
    if name not in ("Edit", "MultiEdit", "NotebookEdit", "Write", "apply_patch", "Bash"):
        return result
    items = []
    for change in tool.get("changes", []):
        try:
            path = Path(change["path"])
            relative = (path if path.is_absolute() else Path(root) / path).resolve().relative_to(Path(root).resolve())
        except (OSError, ValueError, RuntimeError) as exc:
            error("host-action-path", exc)
            continue
        prefix = ("Write " if change["kind"] == "write" else "Edit ") + str(relative)
        diff = redact(change.get("diff", ""))[:200]
        items.append(prefix + (": " + diff if change["kind"] == "edit" and diff else ""))
    current_snapshot = snapshot(root)
    previous_snapshot = result.get("snapshot", "")
    result["snapshot"] = current_snapshot
    groups = result.setdefault("actions", [])
    turn = result.get("turn", 0)
    excerpt = redact(result.get("prompt", ""))[:200]
    group = next((group for group in reversed(groups)
                  if group["turn"] == turn and group.get("prompt") == excerpt), None)
    if name == "Bash" and current_snapshot:
        command = "Bash: " + redact(tool.get("command", ""))
        marker = command + _PENDING
        existing = group["items"] if group else []
        if tool.get("exit_code") not in (None, 0):
            if marker not in existing:
                items.append(marker)
        elif marker in existing:
            existing.remove(marker)
            items.append(command + " failed → ok")
        elif previous_snapshot and current_snapshot != previous_snapshot:
            items.append(command + " (ok)")
    if items:
        if group is None:
            group = {"turn": turn, "prompt": excerpt, "items": []}
            groups.append(group)
        for item in items:
            if item.startswith("Edit "):
                prefix, _, diff = item.partition(": ")
                previous = next((i for i, old in enumerate(group["items"])
                                 if old.partition(": ")[0] == prefix), None)
                if previous is not None:
                    old = group["items"][previous].partition(": ")[2]
                    if diff and diff not in old.split(" · "):
                        group["items"][previous] = prefix + ": " + (old + " · " if old else "") + diff
                    continue
            if item not in group["items"]:
                group["items"].append(item)
    result["actions"] = groups[-5:]
    return result


def detach_args() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
