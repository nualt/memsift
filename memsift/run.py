"""Host-safe entry point and detached, captured background work.

An unwritable debug log cannot record its own failure; hooks still return zero
without context in that case.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memsift import core, doctor, host, store
from memsift.vendor import env_file, logfiles
from memsift.vendor.redact import redact

_EVENTS = {"start": "SessionStart", "prompt": "UserPromptSubmit", "tool": "PostToolUse",
           "stop": "Stop", "end": "SessionEnd"}
_workers = []


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid runner arguments")


def _log(exc):
    try:
        logfiles.error("runner", exc, getattr(exc, "service", ""), getattr(exc, "status", 0),
                       getattr(exc, "duration", 0.0), getattr(exc, "response", ""))
    except Exception as log_exc:
        logfiles.append_line(store.home() / "debug.log", "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__) +
            traceback.format_exception(type(log_exc), log_exc, log_exc.__traceback__)))


def spawn(work: dict, host_name: str) -> None:
    directory = store.home()
    directory.mkdir(parents=True, exist_ok=True)
    # An unlinked file avoids both a large pipe write and persistent worker inputs.
    with tempfile.TemporaryFile(mode="w+b", dir=directory) as stream:
        stream.write(json.dumps(redact(work), ensure_ascii=False).encode("utf-8"))
        stream.seek(0)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), work["event"]["event"],
             "--host", host_name, "--worker"], stdin=stream,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, **host.detach_args())
    _workers[:] = [worker for worker in _workers if worker.poll() is None]
    _workers.append(child)


def _payload(kind):
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Hook payload must be an object")
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        raise ValueError("Hook requires session_id")
    for field in ("cwd", "source", "prompt", "last_assistant_message", "tool_name"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError("Invalid hook field: " + field)
    if payload.get("transcript_path") is not None and not isinstance(payload["transcript_path"], str):
        raise ValueError("Invalid transcript_path")
    if kind == "prompt" and "prompt" not in payload:
        raise ValueError("Prompt hook requires prompt")
    if kind == "start" and not payload.get("cwd"):
        raise ValueError("Session start requires cwd")
    if kind == "tool" and (not payload.get("tool_name") or not isinstance(payload.get("tool_input"), dict)):
        raise ValueError("Tool hook requires tool_name and tool_input")
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("event", choices=list(_EVENTS) + ["doctor", "resume"])
        parser.add_argument("--host", choices=("claude-code", "codex"), default="claude-code")
        parser.add_argument("--cwd")
        parser.add_argument("--worker", action="store_true")
        args = parser.parse_args(argv)
        if args.event in ("doctor", "resume"):
            if args.worker:
                raise ValueError("Commands cannot run as hook workers")
            root = store.repo_root(args.cwd or str(Path.cwd()))
            config = env_file.load_config()
            if args.event == "doctor":
                output = doctor.check(root, config)
            else:
                work = None
                if not sys.stdin.isatty():
                    text = sys.stdin.read()
                    if text.strip():
                        payload = json.loads(text)
                        normalized = host.normalize("UserPromptSubmit", payload, args.host)
                        saved = store.read_json(store.session_path(normalized["session_id"]), {})
                        work = {"event": normalized, "session": saved, "turn": None,
                                "repo_root": str(root)}
                output = core.resume(root, config, work=work)
        else:
            if args.worker:
                work = json.load(sys.stdin)
                if (not isinstance(work, dict) or work.get("event", {}).get("event") != args.event
                        or work["event"].get("host") != args.host or not work.get("repo_root")
                        or not isinstance(work.get("session"), dict)):
                    raise ValueError("Invalid captured worker input")
            else:
                payload = _payload(args.event)
                name = _EVENTS[args.event]
                if args.event == "tool" and payload.get("hook_event_name") == "PostToolUseFailure":
                    name = "PostToolUseFailure"
                work = core.prepare(host.normalize(name, payload, args.host))
                if args.event in ("start", "stop", "end"):
                    spawn(work, args.host)
                    return 0
            config = env_file.load_config()
            output = host.render(work["event"], core.dispatch(work, config))
        if output:
            print(redact(output))
    except Exception as exc:
        _log(exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
