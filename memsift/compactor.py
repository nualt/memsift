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
from . import transport
from .vendor.file_lock import file_lock
from .vendor.logfiles import decision, error
from .vendor.redact import redact

HTTP_TIMEOUT = 60.0
SECTIONS = (("in_progress", "In progress"), ("decisions", "Decisions"),
            ("project_facts", "Project facts"), ("working_rules", "Working rules"))
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "action": {"type": "string", "enum": ["replace", "drop"]},
                           "text": {"type": "string"}},
            "required": ["id", "action", "text"]}},
        "new": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"section": {"type": "string", "enum": [key for key, _ in SECTIONS]},
                           "text": {"type": "string"}, "date": {"type": "string"}},
            "required": ["section", "text", "date"]}},
        "packet": {"type": "object", "additionalProperties": False,
                   "properties": {"topics": {"type": "string"}, "text": {"type": "string"}},
                   "required": ["topics", "text"]},
    },
    "required": ["changes", "new", "packet"],
}
# The model never rewrites the digest: it names the old lines to replace or drop, and code
# keeps every other line word for word. A full rewrite at each compaction lost a user's
# reason once in a test run; an unmentioned line can no longer be shortened.
INSTRUCTIONS = (
    "You maintain a project digest. You receive the current digest, whose lines carry ids [gN], and new "
    "draft entries [dN]. Do not rewrite the digest. "
    "Old lines: every line you do not mention is kept word for word. Mention a line only to replace it "
    "(a new entry contradicts or corrects it, or completes it: write the full new line) or to drop it "
    "(no longer true, or work in progress now done). When two old lines say the same thing, replace one "
    "with the merged line and drop the other. "
    "New lines: one line per new decision, fact, constraint, rule or piece of work in progress from the "
    "draft entries of nature project that the digest does not already hold. Keep the user's words, and "
    "keep the user's reason whenever one is given. An option only considered is written as considered. "
    "Lessons never go to the digest. "
    "Apply each confirmed correction by replacing the line it corrects. Leave pending corrections out; "
    "code carries them over. "
    "Packet: only the new draft entries, cleaned and merged, lessons included, never the old digest again. "
    "Write in the language of the entries. Do not write ids or formatted - [gN] or - [dN] records in any text."
)
_TRAILING_DATES = re.compile(r"(?:\s*\(\d{4}-\d{2}-\d{2}\))+\s*$")
_LINE = re.compile(r"^- \[(g\d+)\] (.*) \((\d{4}-\d{2}-\d{2})\)$")


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


def _line(value, label):
    _text(value, label)
    if re.search(r"^\s*-\s*\[[gd]\d+\]", value, re.MULTILINE):
        raise ValueError(label + " impersonates a formatted memory ID")


def validate(answer, notes):
    _object(answer, ("changes", "new", "packet"), "answer")
    for field in ("changes", "new"):
        if not isinstance(answer[field], list):
            raise ValueError(field + " must be an array")
    for change in answer["changes"]:
        _object(change, ("id", "action", "text"), "change")
        if change["action"] not in ("replace", "drop") or not isinstance(change["id"], str):
            raise ValueError("change requires an id and replace or drop")
        if change["action"] == "replace":
            _line(change["text"], "replacement text")
    for line in answer["new"]:
        _object(line, ("section", "text", "date"), "new line")
        if line["section"] not in dict(SECTIONS):
            raise ValueError("new line section is unknown")
        _line(line["text"], "new line text")
        if not isinstance(line["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", line["date"]):
            raise ValueError("new line date must be YYYY-MM-DD")
        date.fromisoformat(line["date"])
    _object(answer["packet"], ("topics", "text"), "packet")
    _topics(answer["packet"]["topics"])
    _text(answer["packet"]["text"], "packet text")
    knowledge = (re.search(r"^- \[g\d+\]", notes.get("digest", ""), re.MULTILINE)
                 or any(entry["id"].startswith("d") and entry["nature"] == "project"
                        and not entry["pending"] for entry in notes.get("entries", [])))
    if knowledge and not any(apply(notes.get("digest", ""), answer).values()):
        raise ValueError("digest is empty despite existing knowledge")
    return answer


def apply(digest, answer):
    """Old digest text plus the model's changes -> sections. Unknown ids are ignored, never guessed."""
    sections = {key: [] for key, _ in SECTIONS}
    changes = {change["id"]: change for change in answer["changes"]}
    headings = {heading: key for key, heading in SECTIONS}
    section = None
    for raw in digest.splitlines():
        if raw.startswith("## "):
            section = headings.get(raw[3:].strip())
        match = _LINE.match(raw)
        if not (match and section):
            continue
        identifier, text, day = match.groups()
        change = changes.get(identifier)
        if change is None:
            sections[section].append({"text": text, "date": day})
        elif change["action"] == "replace":
            sections[section].append({"text": change["text"], "date": date.today().isoformat()})
    for line in answer["new"]:
        sections[line["section"]].append({"text": line["text"], "date": line["date"]})
    return sections


def render_digest(root, digest, ratio):
    sections = {key: sorted(digest[key], key=lambda entry: entry["date"], reverse=True)
                for key, _ in SECTIONS}
    reset = False
    while True:
        lines = ["# Digest: " + store.repo_key(root)]
        identifier = 0
        for key, heading in SECTIONS:
            lines.append("## " + heading)
            for entry in sections[key]:
                identifier += 1
                # The model sometimes copies a line's date into its text; code adds the date itself.
                text = _TRAILING_DATES.sub("", " ".join(entry["text"].splitlines()))
                lines.append("- [g%d] %s (%s)" % (identifier, text, entry["date"]))
        rendered = redact("\n".join(lines) + "\n")
        if store.estimate(rendered, ratio) <= 3000:
            if reset:
                decision("digest-reset", {"kept": identifier})
            return rendered
        # ponytail: at the cap the digest is emptied but for its working rules, which apply to
        # every prompt; every dropped line already went to Cognee in the packet of its day.
        if not reset:
            reset = True
            sections = {key: entries if key == "working_rules" else [] for key, entries in sections.items()}
        elif sections["working_rules"]:
            sections["working_rules"].pop()
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
                response = transport.open_request(wire, timeout=max(0.001, deadline - time.monotonic()))
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
                              "name": "digest_edits", "strict": True, "schema": SCHEMA}}})
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
            digest = render_digest(root, apply(notes["digest"], answer), ratio)
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
