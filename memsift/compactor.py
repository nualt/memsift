"""Validated background compaction with durable packets and append preservation."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request

from . import breaker, cognee, store
from .vendor.file_lock import file_lock
from .vendor.logfiles import decision, error
from .vendor.redact import redact

HTTP_TIMEOUT = 60.0
SECTIONS = (("in_progress", "In progress"), ("decisions", "Decisions"),
            ("project_facts", "Project facts"), ("working_rules", "Working rules"))
ENTRY_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"text": {"type": "string"}, "date": {"type": "string"}},
                "required": ["text", "date"]}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "digest": {"type": "object", "additionalProperties": False,
                   "properties": {key: {"type": "array", "items": ENTRY_SCHEMA} for key, _ in SECTIONS},
                   "required": [key for key, _ in SECTIONS]},
        "packet": {"type": "object", "additionalProperties": False,
                   "properties": {"topics": {"type": "string"}, "text": {"type": "string"}},
                   "required": ["topics", "text"]},
    },
    "required": ["digest", "packet"],
}
INSTRUCTIONS = (
    "Keep the user's words. Invent nothing, add nothing. Merge duplicates. "
    "When two entries contradict each other, the most recent wins. "
    "Apply each confirmed correction: replace the statement it corrects. "
    "Leave pending corrections out; code carries them over. "
    "Lessons go to the packet only, never to the digest. "
    "Digest: work in progress first, then most recent first. Drop what is no longer true. "
    "Write in the language of the entries. "
    "Packet: only new draft entries, cleaned and merged, lessons included, never the old digest again. "
    "Do not supply entry IDs or formatted - [gN] or - [dN] records."
)


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + " requires exactly " + ", ".join(fields))


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonblank text")


def _topics(value):
    # A packet name is only a label: an untidy topic line is tidied, never a reason
    # to throw away a whole compaction. Separators and line breaks become spaces,
    # double quotes become single ones, other controls are dropped, and only the
    # first four words are kept.
    _text(value, "packet topics")
    value = value.replace('"', "'")
    value = "".join(" " if char in "/\\\n\r\t\0" else "" if unicodedata.category(char) == "Cc" else char
                    for char in value)
    words = value.split()
    if not words:
        raise ValueError("packet topics are empty")
    return " ".join(words[:4])


def validate(answer, notes):
    _object(answer, ("digest", "packet"), "answer")
    digest = answer["digest"]
    _object(digest, [key for key, _ in SECTIONS], "digest")
    for key, _ in SECTIONS:
        if not isinstance(digest[key], list):
            raise ValueError("digest " + key + " must be an array")
        for entry in digest[key]:
            _object(entry, ("text", "date"), "digest entry")
            _text(entry["text"], "digest entry text")
            if re.search(r"^\s*-\s*\[[gd]\d+\]", entry["text"], re.MULTILINE):
                raise ValueError("digest entry impersonates a formatted memory ID")
            if not isinstance(entry["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["date"]):
                raise ValueError("digest entry date must be YYYY-MM-DD")
            date.fromisoformat(entry["date"])
    _object(answer["packet"], ("topics", "text"), "packet")
    _topics(answer["packet"]["topics"])
    _text(answer["packet"]["text"], "packet text")
    knowledge = (re.search(r"^- \[g\d+\]", notes.get("digest", ""), re.MULTILINE)
                 or any(entry["id"].startswith("d") and entry["nature"] == "project"
                        and not entry["pending"] for entry in notes.get("entries", [])))
    if knowledge and not any(digest.values()):
        raise ValueError("digest is empty despite existing knowledge")
    return answer


def render_digest(root, digest, ratio):
    sections = {key: sorted(digest[key], key=lambda entry: entry["date"], reverse=True)
                for key, _ in SECTIONS}
    while True:
        lines = ["# Digest: " + store.repo_key(root)]
        identifier = 0
        for key, heading in SECTIONS:
            lines.append("## " + heading)
            for entry in sections[key]:
                identifier += 1
                text = " ".join(entry["text"].splitlines())
                lines.append("- [g%d] %s (%s)" % (identifier, text, entry["date"]))
        rendered = redact("\n".join(lines) + "\n")
        if store.estimate(rendered, ratio) <= 3000:
            return rendered
        for key, _ in reversed(SECTIONS):
            if sections[key]:
                sections[key].pop()
                break
        else:
            raise ValueError("digest header exceeds the token budget")


def packet_name(root, topics, now):
    topics = _topics(topics)
    return redact("%s %s %s %s.txt" % (Path(root).name, now.strftime("%Y-%m-%d"),
                                      now.strftime("%Hh%M"), topics))


def _http(wire, started):
    """Bound the complete response, including a body that never stops trickling."""
    deadline = started + HTTP_TIMEOUT
    done = threading.Event()
    result = {"status": 0, "response": ""}

    def send():
        try:
            cause = None
            try:
                response = urllib.request.urlopen(wire, timeout=max(0.001, deadline - time.monotonic()))
            except urllib.error.HTTPError as exc:
                error("compactor-http", exc, "compactor", exc.code, time.monotonic() - started)
                response, cause = exc, exc
            with response:
                result["status"] = response.status
                chunks = []
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("compactor body deadline expired")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    result["response"] = b"".join(chunks)[:1200].decode("utf-8", "replace")[:300]
                result["response"] = b"".join(chunks).decode("utf-8")
                if response.status >= 400:
                    raise breaker.ServiceError("compactor", "HTTP %s" % response.status,
                                               response.status, result["response"], time.monotonic() - started) from cause
        except Exception as exc:
            error("compactor-transport", exc, "compactor", result["status"],
                  time.monotonic() - started, result["response"])
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=send, name="memsift-compactor-http", daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        raise breaker.ServiceError("compactor", "compactor request deadline expired",
                                   result["status"], result["response"], time.monotonic() - started)
    if "error" in result:
        exc = result["error"]
        raise breaker.ServiceError("compactor", str(exc), result["status"], result["response"],
                                   time.monotonic() - started) from exc
    return result["status"], result["response"]


def request(config, notes, *, probe=False):
    started = time.monotonic()
    status, raw = 0, ""
    try:
        if not probe:
            breaker.require_active("compactor")
        payload = redact({"model": config["MEMSIFT_COMPACTOR_MODEL"], "temperature": 0.2,
                          "messages": [{"role": "system", "content": INSTRUCTIONS},
                                       {"role": "user", "content": notes["digest"] + "\n\n" + notes["draft"]}],
                          "response_format": {"type": "json_schema", "json_schema": {
                              "name": "memory_compaction", "strict": True, "schema": SCHEMA}}})
        wire = urllib.request.Request(config["MEMSIFT_COMPACTOR_URL"].rstrip("/") + "/chat/completions",
                                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                      headers={"Authorization": "Bearer " + config["MEMSIFT_COMPACTOR_API_KEY"],
                                               "Content-Type": "application/json"}, method="POST")
        status, raw = _http(wire, started)
        body = json.loads(raw)
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("compactor response lacks choices[0]")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("compactor response lacks message content")
        return validate(json.loads(message["content"]), notes)
    except Exception as exc:
        status = getattr(exc, "status", status)
        raw = getattr(exc, "response", raw)
        error("compactor-request", exc, "compactor", status, time.monotonic() - started, raw)
        if isinstance(exc, breaker.PausedError) or (isinstance(exc, ValueError) and status):
            raise
        raise breaker.ServiceError("compactor", str(exc), status, raw, time.monotonic() - started) from exc


def compact(root, config, *, force=False):
    started = time.monotonic()
    attempted = False
    try:
        with file_lock(store.repo_dir(root) / "compaction.lock") as acquired:
            if not acquired:
                return False
            breaker.require_active("jev")
            breaker.require_active("compactor")
            ratio = breaker.ratio()
            with file_lock(store.repo_dir(root) / "draft.md.lock", timeout=store.LOCK_WAIT) as acquired:
                if not acquired:
                    raise TimeoutError("compaction snapshot lock unavailable")
                notes = store.notes_snapshot(root)
            entries = [entry for entry in notes["entries"] if entry["id"].startswith("d") and not entry["pending"]]
            if not entries or (not force and store.estimate(notes["digest"] + notes["draft"], ratio) < 8000):
                return False
            for attempt in range(2):
                try:
                    attempted = True
                    answer = validate(request(config, notes), notes)
                    break
                except ValueError as exc:
                    error("compactor-invalid", exc, "compactor", duration=time.monotonic() - started)
                    if attempt:
                        raise
            digest = render_digest(root, answer["digest"], ratio)
            packet = cognee.queue_packet(root, packet_name(root, answer["packet"]["topics"], datetime.now(timezone.utc)),
                                         answer["packet"]["text"])
            store.replace_notes(root, notes, digest)
            try:
                breaker.record("compactor", {"kind": "success", "reason": "", "status": 200})
            except Exception as exc:
                error("compactor-accounting", exc, "compactor", duration=time.monotonic() - started)
            decision("compaction", {"packet": packet.name, "digest_tokens": store.estimate(digest, ratio),
                                    "duration": time.monotonic() - started})
            try:
                cognee.send_outbox(store.repo_dir(root), config)
            except Exception as exc:
                error("compactor-outbox", exc, "cognee", duration=time.monotonic() - started)
            return True
    except breaker.PausedError as exc:
        error("compactor-paused", exc, exc.service, duration=time.monotonic() - started)
    except Exception as exc:
        error("compaction", exc, "compactor", getattr(exc, "status", 0),
              time.monotonic() - started, getattr(exc, "response", ""))
        if attempted:
            try:
                breaker.record("compactor", breaker.classify(exc))
            except Exception as accounting_error:
                error("compactor-accounting", accounting_error, "compactor", duration=time.monotonic() - started)
    return False


def probe(config):
    notes = {"digest": "", "draft": "- [d1] %s lesson\n  User: acme-shop probe works.\n" % date.today().isoformat(),
             "entries": [], "pending": {}}
    try:
        validate(request(config, notes, probe=True), notes)
    except Exception as exc:
        error("compactor-probe", exc, "compactor", getattr(exc, "status", 0),
              getattr(exc, "duration", 0.0), getattr(exc, "response", ""))
        raise
