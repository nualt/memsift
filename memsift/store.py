"""Redacted shared notes and atomic local storage."""

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from contextlib import contextmanager
from typing import Optional

from .vendor.file_lock import file_lock
from .vendor.logfiles import error
from .vendor.redact import redact

RATIO_START = 3.0
PROMPT_TOKENS = 1500
ANSWER_TOKENS = 1000
SESSION_AGE = 7 * 24 * 60 * 60
LOCK_WAIT = 1.0
NATURES = {"project", "lesson"}
_ENTRY = re.compile(r"^- \[([dg]\d+)\] (.*)", re.MULTILINE)


def home() -> Path:
    return Path.home() / ".memsift"


def repo_root(cwd: str) -> Path:
    root = Path(cwd).resolve()
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=LOCK_WAIT)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        error("store-repo-root", exc)
    return root


def repo_key(root: Path) -> str:
    root = Path(root).resolve()
    return root.name + "-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:6]


def repo_dir(root: Path) -> Path:
    return home() / "repos" / repo_key(root)


def session_path(session_id: str) -> Path:
    if (not session_id or session_id == "." or ".." in session_id or
            any(char in session_id for char in ("/", "\\", "\0"))):
        raise ValueError("Invalid session ID")
    directory = home() / "sessions"
    path = directory / (session_id + ".json")
    if path.resolve().parent != directory.resolve():
        raise ValueError("Session path escapes the sessions directory")
    return path


def read_text(path: Path) -> str:
    path = Path(path)
    try:
        if not path.exists():
            return ""
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        error("store-read-missing", exc)
        return ""
    except (OSError, UnicodeError) as exc:
        error("store-read", exc)
        raise


def read_json(path: Path, default: dict) -> dict:
    text = read_text(path)
    if not text:
        return copy.deepcopy(default)
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Stored JSON must be an object")
        return value
    except ValueError as exc:
        error("store-read-json", exc)
        return copy.deepcopy(default)


@contextmanager
def _locked(path: Path):
    try:
        with file_lock(path.with_name(path.name + ".lock"), timeout=LOCK_WAIT) as acquired:
            if not acquired:
                raise TimeoutError("Storage file lock unavailable: " + str(path))
            yield
    except Exception as exc:
        error("store-write", exc)
        raise


def _clean(text: str) -> str:
    return redact(text).replace("\r\n", "\n").replace("\r", "\n")


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    with _locked(path):
        _write(path, _clean(text))


def _update(path: Path, default: dict, change) -> dict:
    value = redact(change(read_json(path, default)))
    if not isinstance(value, dict):
        raise ValueError("Storage update must return an object")
    _write(path, json.dumps(value, ensure_ascii=False) + "\n")
    return value


def update_json(path: Path, default: dict, change) -> dict:
    path = Path(path)
    with _locked(path):
        return _update(path, default, change)


def _append(path: Path, text: str) -> None:
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    data = text.encode("utf-8")
    # Open only under the file lock, so replacement cannot orphan this append.
    with path.open("ab", buffering=0) as stream:
        if stream.write(data) != len(data):
            raise OSError("Incomplete storage append")


def append_text(path: Path, text: str) -> None:
    path = Path(path)
    with _locked(path):
        _append(path, _clean(text))


def estimate(text: str, ratio: float) -> int:
    return math.ceil(len(text) / ratio)


def clip(text: str, tokens: int, ratio: float) -> str:
    text = redact(text)
    cap = max(0, math.floor(tokens * ratio))
    if len(text) <= cap:
        return text
    marker = "[…]"
    if cap < len(marker):
        return marker[:cap]
    available = cap - len(marker)
    left = (available + 1) // 2
    right = available // 2
    return text[:left] + marker + (text[-right:] if right else "")


def _entries(text: str) -> list:
    entries = []
    confirmed = set()
    matches = list(_ENTRY.finditer(text))
    for index, match in enumerate(matches):
        identifier, metadata = match.groups()
        if metadata == "confirmed":
            confirmed.add(identifier)
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        original = text[match.start():end]
        correction = " · correction" in metadata
        if identifier.startswith("g"):
            # Section headings belong to the digest, never to a selectable entry.
            heading = re.search(r"^#", original, re.MULTILINE)
            if heading:
                original = original[:heading.start()]
            nature = "project"
            date = re.search(r"\((\d{4}-\d{2}-\d{2})\)$", metadata)
            date = date.group(1) if date else ""
        else:
            fields = metadata.split(" · ", 1)[0].split(" ", 1)
            if len(fields) != 2:
                continue
            date, nature = fields
        entries.append({"id": identifier, "text": original, "date": date,
                        "nature": nature, "correction": correction,
                        "pending": correction and " · pending" in metadata})
    for entry in entries:
        if entry["id"] in confirmed:
            entry["pending"] = False
    return entries


def _notes(digest: str, draft: str) -> dict:
    entries = _entries(digest) + _entries(draft)
    pending = {entry["id"]: entry["text"] for entry in entries if entry["pending"]}
    return {"digest": digest, "draft": draft, "entries": entries, "pending": pending}


def read_notes(root: Path) -> dict:
    directory = repo_dir(root)
    if not directory.exists():
        return _notes("", "")
    with _locked(directory / "draft.md"):
        return _notes(read_text(directory / "digest.md"), read_text(directory / "draft.md"))


def _header(root: Path) -> str:
    return "# Draft: " + repo_key(root) + "\n\n"


def _field(label: str, text: str) -> str:
    return "  " + (label + ": " + text).replace("\n", "\n  ") + "\n"


def append_draft(root: Path, turn: dict, nature: str, correction: bool,
                 ratio: float) -> dict:
    if nature not in NATURES:
        raise ValueError("Invalid draft entry nature")
    path = repo_dir(root) / "draft.md"
    with _locked(path):
        draft = read_text(path)
        numbers = [int(value) for value in re.findall(r"^- \[d(\d+)\]", draft, re.MULTILINE)]
        identifier = "d" + str(max(numbers, default=0) + 1)
        date = turn["ts"][:10]
        text = "- [%s] %s %s%s · timestamp %s\n" % (
            identifier, date, nature, " · correction · pending" if correction else "", turn["ts"])
        text += _field("User", _clean(clip(turn["prompt"], PROMPT_TOKENS, ratio)))
        text += _field("Agent", _clean(clip(turn["answer"], ANSWER_TOKENS, ratio)))
        if turn["actions"]:
            text += _field("Actions", _clean(" · ".join(turn["actions"])))
        text = _clean(text)
        prefix = _header(root) if not draft else ("\n" if not draft.endswith("\n") else "")
        _append(path, _clean(prefix + text))
        return _entries(text)[0]


def confirm(root: Path, expected: dict) -> list:
    path = repo_dir(root) / "draft.md"
    with _locked(path):
        entries = _entries(read_text(path))
        identifiers = [entry["id"] for entry in entries
                       if entry["pending"] and expected.get(entry["id"]) == entry["text"]]
        if identifiers:
            _append(path, "".join("- [%s] confirmed\n" % identifier for identifier in identifiers))
        return identifiers


def queue_turn(root: Path, turn: dict, ratio: float) -> None:
    value = redact(dict(turn))
    value["prompt"] = clip(value["prompt"], PROMPT_TOKENS, ratio)
    value["answer"] = clip(value["answer"], ANSWER_TOKENS, ratio)
    path = repo_dir(root) / "pending.jsonl"
    with _locked(path):
        _append(path, json.dumps(redact(value), ensure_ascii=False) + "\n")


def _turns(text: str) -> list:
    try:
        turns = [json.loads(line) for line in text.splitlines()]
        if any(not isinstance(turn, dict) for turn in turns):
            raise ValueError("Pending turns must be JSON objects")
        return turns
    except ValueError as exc:
        error("store-pending", exc)
        raise


def pending_turns(root: Path) -> list:
    return _turns(read_text(repo_dir(root) / "pending.jsonl"))


def order_pending(root: Path) -> list:
    path = repo_dir(root) / "pending.jsonl"
    with _locked(path):
        turns = _turns(read_text(path))
        ordered = sorted(turns, key=lambda turn: turn["ts"])
        if ordered != turns:
            _write(path, "".join(json.dumps(redact(turn), ensure_ascii=False) + "\n" for turn in ordered))
        return ordered


@contextmanager
def replay_lock(root: Path):
    with file_lock(repo_dir(root) / "replay.lock", timeout=0.0) as acquired:
        yield acquired


def update_session_if_exists(session_id: str, change) -> Optional[dict]:
    path = session_path(session_id)
    with _locked(path):
        if not path.exists():
            return None
        return _update(path, {}, change)


def ack_pending(root: Path, processed: list) -> None:
    if not processed:
        return
    path = repo_dir(root) / "pending.jsonl"
    with _locked(path):
        text = read_text(path)
        turns = _turns(text)
        try:
            if turns[:len(processed)] != processed:
                raise ValueError("Pending acknowledgement does not match the exact prefix")
        except ValueError as exc:
            error("store-ack-prefix", exc)
            return
        remaining = "".join(json.dumps(redact(turn), ensure_ascii=False) + "\n"
                            for turn in turns[len(processed):])
        if remaining:
            _write(path, remaining)
        elif path.exists():
            path.unlink()


def notes_snapshot(root: Path) -> dict:
    notes = read_notes(root)
    notes["draft_bytes"] = len(notes["draft"].encode("utf-8"))
    return notes


def replace_notes(root: Path, snapshot: dict, digest: str) -> None:
    directory = repo_dir(root)
    path = directory / "draft.md"
    with _locked(path):
        # Crash order is intentional: the compactor has already queued the packet.
        write_text(directory / "digest.md", digest)
        current = read_text(path).encode("utf-8")
        original = snapshot["draft"].encode("utf-8")
        if not current.startswith(original) or snapshot["draft_bytes"] != len(original):
            raise ValueError("Draft no longer matches the compaction snapshot")
        tail = current[snapshot["draft_bytes"]:].decode("utf-8")
        header = _header(root)
        if not original and tail.startswith(header):
            tail = tail[len(header):]
        pending = "".join(entry["text"] for entry in snapshot["entries"]
                          if snapshot["pending"].get(entry["id"]) == entry["text"])
        # Keep snapshot-pending corrections even if a tail status confirmed them.
        _write(path, _clean(header + pending + tail))


def prune_sessions(now: float) -> None:
    for path in (home() / "sessions").glob("*.json"):
        with _locked(path):
            if path.exists() and path.stat().st_mtime < now - SESSION_AGE:
                path.unlink()
