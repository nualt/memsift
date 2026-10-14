"""Memory lifecycle, captured background work and explicit service recovery."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Optional

from . import breaker, cognee, compactor, host, jev, store
from .vendor.file_lock import file_lock
from .vendor.logfiles import decision, error
from .vendor.redact import redact

PROMPT_DEADLINE = 16.0

SESSION_START = {
    "startup": "new session: the agent knows nothing of this repo",
    "clear": "new session: the agent knows nothing of this repo",
    "compact": "only the compaction summary and what followed",
    "resume": "the whole previous conversation",
}
FLAG_MESSAGES = {
    "jev_md_missing": "This repo has no jev.md. Propose a first version to the user, with exactly these five lines and nothing else, the file starting with the two comment lines '# jev.md · project card for memsift: the five lines below only.' and '# Decisions, rules and facts are kept by memsift, not here.': project (what this repo is, in one line), stakes (where the important decisions are), phase (where the project stands), documented in (files that already hold knowledge), do not keep (what never needs remembering). Do not put decisions, rules or facts in it: memsift keeps those.",
    "jev_md_outdated": "jev.md looks outdated. Propose an update to the user, keeping exactly these five lines and nothing else, the file starting with the two comment lines '# jev.md · project card for memsift: the five lines below only.' and '# Decisions, rules and facts are kept by memsift, not here.': project (what this repo is, in one line), stakes (where the important decisions are), phase (where the project stands), documented in (files that already hold knowledge), do not keep (what never needs remembering). Do not put decisions, rules or facts in it: memsift keeps those.",
}


def _log(event, exc):
    error(event, exc, getattr(exc, "service", ""), getattr(exc, "status", 0),
          getattr(exc, "duration", 0.0), getattr(exc, "response", ""))


def _visible(items):
    return [item for item in items if not item.endswith(" (failed pending)")]


def prepare(event: dict) -> dict:
    event = redact(copy.deepcopy(event))
    kind = event["event"]
    if kind not in ("start", "prompt", "tool", "stop", "end"):
        raise ValueError("Unsupported memory event")
    sid = event["session_id"]
    path = store.session_path(sid)
    if kind == "start":
        if not event.get("cwd"):
            raise ValueError("Session start requires cwd")
        root = store.repo_root(event["cwd"])
        source = event.get("source") or "startup"
        source = "resume" if source == "fork" else source
        if source not in SESSION_START:
            raise ValueError("Unsupported session source")
        event["source"] = source
        session = {"repo_key": store.repo_key(root), "repo_root": str(root),
                   "host": event["host"], "source": source,
                   "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "turn": 0, "prompt": "", "snapshot": host.snapshot(root), "actions": [],
                   "flags": [] if (root / "jev.md").is_file() else ["jev_md_missing"],
                   "outage_notices": []}
        store.prune_sessions(time.time())
        session = store.update_json(path, {}, lambda _: session)
    else:
        if kind == "end":
            with file_lock(path.with_name(path.name + ".lock"), timeout=store.LOCK_WAIT) as acquired:
                if not acquired:
                    raise TimeoutError("Session end lock unavailable")
                session = store.read_json(path, {})
                if path.exists():
                    path.unlink()
        else:
            session = store.read_json(path, {})
        if session.get("repo_root"):
            root = Path(session["repo_root"])
        elif event.get("cwd"):
            root = store.repo_root(event["cwd"])
        else:
            raise ValueError("Missing saved repo root and cwd")
        if kind == "prompt":
            def save(value):
                value["turn"] += 1
                value["prompt"] = event["prompt"]
                value["actions"] = [group for group in value.get("actions", [])
                                    if value["turn"] - 5 < group["turn"] <= value["turn"]]
                return value
            session = store.update_session_if_exists(sid, save) or session
    session = copy.deepcopy(session)
    session["actions"] = [group for group in session.get("actions", [])
                          if session.get("turn", 0) - 5 < group["turn"] <= session.get("turn", 0)][-5:]
    captured_turn = None
    if kind == "stop":
        items = [item for group in session["actions"]
                 if group["turn"] == session.get("turn")
                 and group.get("prompt") == session.get("prompt", "")[:200]
                 for item in group["items"]]
        captured_turn = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "session": sid, "prompt": session.get("prompt", ""),
                         "answer": event["answer"], "actions": _visible(items)}
    return {"event": event, "session": session, "turn": captured_turn, "repo_root": str(root)}


def dispatch(work: dict, config: dict) -> str:
    handlers = {"start": start, "prompt": prompt, "tool": tool, "stop": stop, "end": end}
    return handlers[work["event"]["event"]](work, config)


def _add_flags(work, flags):
    sid = work["event"].get("session_id")
    if not flags or not sid:
        return
    def add(session):
        session["flags"] = list(dict.fromkeys(session.get("flags", []) + flags))
        return session
    store.update_session_if_exists(sid, add)


def start(work: dict, config: dict) -> str:
    if breaker.paused("jev") or breaker.paused("cognee"):
        return ""
    root = Path(work["repo_root"])
    try:
        missing = cognee.rebuild_index(root, config)
        _add_flags(work, ["dataset_undescribed:" + name for name in missing])
    except Exception as exc:
        _log("core-start-index", exc)
    cognee.send_outbox(store.repo_dir(root), config)
    return ""


def _conversation(work, ratio):
    return host.read_conversation(Path(work["event"].get("transcript_path") or ""),
                                  work["event"].get("host", "claude-code"),
                                  work["session"].get("actions", [])[-5:], ratio)


def _advisories(work):
    messages = []
    def consume(session):
        services = breaker.state()["services"]
        remaining = []
        for flag in session.get("flags", []):
            message = FLAG_MESSAGES.get(flag)
            if flag.startswith("dataset_undescribed:"):
                message = "Cognee dataset `%s` has no line in datasets.md. Propose a description to the user, or IGNORED." % flag.split(":", 1)[1]
            elif flag.startswith("decision_reason_missing:"):
                message = "Entry `%s` records a structuring decision without its reason. Ask the user why." % flag.split(":", 1)[1]
            if message:
                messages.append(message)
            else:
                remaining.append(flag)
        session["flags"] = remaining
        notices = session.setdefault("outage_notices", [])
        for service, state in services.items():
            if not state["paused"] or service in notices:
                continue
            failures = state["failures"]
            if service == "jev":
                message = "memsift is paused: Jev stopped answering (%s failures: %s). Memory is off. Tell the user; they can run /memsift-resume once it is fixed." % (failures, state["reason"])
            elif service == "cognee":
                message = "memsift: Cognee stopped answering (%s failures). Memory keeps working from the local notes; packets wait to be sent. Tell the user; /memsift-resume restarts it." % failures
            else:
                message = "memsift: the compactor stopped answering (%s failures). Notes are no longer compacted. Tell the user; /memsift-resume restarts it." % failures
            messages.append(message)
            notices.append(service)
        return session
    sid = work["event"].get("session_id")
    if sid:
        store.update_session_if_exists(sid, consume)
    return "memsift:\n" + "\n".join("- " + message for message in messages) if messages else ""


def _answers(answers):
    # Compact view of Jev's answers for debug.log: the probabilities behind each decision.
    view = {}
    for name, answer in answers.items():
        if answer.get("type") == "noul":
            view[name] = round(answer["noul"], 2)
        elif answer.get("type") == "choice":
            view[name] = {"choice": answer["choice"], "confidence": round(answer["confidence"], 2),
                          "probabilities": {k: round(v, 2) for k, v in answer["probabilities"].items()}}
        elif answer.get("type") == "score":
            view[name] = {"probabilities": {answer["legend"].get(k, k): round(v, 2)
                                            for k, v in answer["probabilities"].items()}}
    return view


def _prompt_memory(work, config, deadline):
    root = Path(work["repo_root"])
    ratio = breaker.ratio()
    notes = store.read_notes(root)
    conversation = _conversation(work, ratio)
    descriptions = cognee.descriptions()
    index = store.read_json(store.repo_dir(root) / "cognee-index.json", {"datasets": []})
    rows = {row["name"]: row for row in index["datasets"]}
    datasets = {name: {"description": descriptions.get(name, ""), "items": rows.get(name, {}).get("items", [])}
                for name in dict.fromkeys(list(descriptions) + list(rows))
                if descriptions.get(name) != "IGNORED"}
    state = {"prompt": work["event"]["prompt"], "conversation": conversation["exchanges"],
             "notes": {entry["id"]: entry["text"] for entry in notes["entries"]}, "datasets": datasets}
    answers = jev.request_prompt(config, state, timeout=_remaining(deadline, jev.PROMPT_TIMEOUT))
    choice = jev.choose_prompt(answers, list(state["notes"]), list(state["datasets"]))
    decision("core-prompt", {"prompt": work["event"]["prompt"][:80], "answers": _answers(answers), **choice})
    if choice["entries"]:
        return "memsift, from the working notes:\n" + "".join(state["notes"][identifier] for identifier in choice["entries"])
    if not choice["recall"] or breaker.paused("cognee"):
        return ""
    passages = cognee.recall(config, state["prompt"],
                             [rows.get(name, {"name": name, "id": ""}) for name in choice["datasets"]],
                             timeout=_remaining(deadline, 12.0))
    if not passages:
        return ""
    passage_state = {"prompt": state["prompt"],
                     "passages": {passage["id"]: passage["text"] for passage in passages}}
    answers = jev.request(config, {"prompt": state["prompt"]}, jev.passage_questions(passage_state),
                         timeout=_remaining(deadline, jev.PASSAGE_TIMEOUT))
    kept = jev.choose_passages(answers, passages)
    blocks = []
    for name in dict.fromkeys(passage["dataset"] for passage in kept):
        blocks.append("memsift, from Cognee (dataset %s):\n%s" % (
            name, "\n".join(passage["text"] for passage in kept if passage["dataset"] == name)))
    return "\n\n".join(blocks)


def _remaining(deadline, cap):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Prompt deadline expired")
    return min(cap, remaining)


def prompt(work: dict, config: dict) -> str:
    deadline = time.monotonic() + PROMPT_DEADLINE
    result = {}
    completed = threading.Event()

    def read_context():
        try:
            memory = ""
            if not breaker.paused("jev"):
                try:
                    memory = _prompt_memory(work, config, deadline)
                except Exception as exc:
                    _log("core-prompt", exc)
            _remaining(deadline, PROMPT_DEADLINE)
            result["text"] = "\n\n".join(block for block in (memory, _advisories(work)) if block)
        except Exception as exc:
            _log("core-prompt", exc)
        finally:
            completed.set()

    threading.Thread(target=read_context, daemon=True).start()
    if not completed.wait(max(0, deadline - time.monotonic())):
        try:
            raise TimeoutError("Prompt deadline expired during local or service work")
        except TimeoutError as exc:
            _log("core-prompt", exc)
        return ""
    return result.get("text", "")


def tool(work: dict, config: dict) -> str:
    sid = work["event"]["session_id"]
    captured = work["session"]
    deadline = time.monotonic() + store.LOCK_WAIT
    while captured:
        updated = host.note_tool(Path(work["repo_root"]), captured, work["event"]["tool"])
        stale = False
        def merge(current):
            nonlocal stale
            if (current.get("turn"), current.get("prompt")) != (captured.get("turn"), captured.get("prompt")):
                return current
            if any(current.get(key) != captured.get(key) for key in ("snapshot", "actions")):
                stale = True
                return current
            current["snapshot"] = updated["snapshot"]
            current["actions"] = updated["actions"]
            return current
        current = store.update_session_if_exists(sid, merge)
        if not stale or current is None:
            return ""
        if time.monotonic() >= deadline:
            raise TimeoutError("Concurrent action updates exceeded the local deadline")
        captured = current
    return ""


def _classify_turn(work: dict, turn: dict, config: dict) -> bool:
    root = Path(work["repo_root"])
    ratio = breaker.ratio()
    notes = store.read_notes(root)
    state = {"turn": {key: turn[key] for key in ("prompt", "answer", "actions")},
             "recent_actions": [item for group in work["session"].get("actions", [])[-5:]
                                for item in _visible(group["items"])],
             "project_card": store.read_text(root / "jev.md"),
             "instruction_files": "\n\n".join(store.read_text(root / name) for name in ("CLAUDE.md", "AGENTS.md")),
             "notes": {entry["id"]: entry["text"] for entry in notes["entries"]},
             "pending_corrections": notes["pending"]}
    if notes["pending"]:
        if work["event"].get("transcript_path"):
            state["recent_exchanges"] = _conversation(work, ratio)["exchanges"]
        elif work["event"].get("event") != "stop":
            state["pending_corrections"] = {}
    attempted = False
    try:
        state = jev.fit_stop(state, jev.stop_questions(state), ratio)
        attempted = True
        answers = jev.request(config, state, jev.stop_questions(state), timeout=jev.STOP_TIMEOUT)
        choice = jev.choose_stop(answers, list(state["pending_corrections"]))
    except Exception as exc:
        _log("core-classify", exc)
        if not attempted and not isinstance(exc, breaker.PausedError):
            breaker.record("jev", breaker.classify(exc))
        if isinstance(exc, (breaker.ServiceError, breaker.PausedError)):
            raise
        raise breaker.ServiceError("jev", str(exc)) from exc
    if choice["confirmed"]:
        store.confirm(root, {identifier: notes["pending"][identifier] for identifier in choice["confirmed"]})
    entry = None
    if choice["keep"]:
        entry = store.append_draft(root, turn, choice["nature"], choice["correction"], breaker.ratio())
    flags = ["jev_md_outdated"] if choice["card_outdated"] else []
    if entry and choice["reason_missing"]:
        flags.append("decision_reason_missing:" + entry["id"])
    try:
        _add_flags(work, flags)
    except Exception as exc:
        # A session advisory failure must not replay an already durable entry.
        _log("core-stop-flags", exc)
    try:
        decision("core-stop", {"prompt": turn["prompt"][:80], "answers": _answers(answers), **choice})
    except Exception as exc:
        _log("core-stop-decision", exc)
    return choice["keep"]


def replay_pending(work: dict, config: dict) -> dict:
    root = Path(work["repo_root"])
    result = {"sorted": 0, "kept": 0}
    with store.replay_lock(root) as acquired:
        if not acquired or breaker.paused("jev"):
            return result
        processed = []
        try:
            pending = store.order_pending(root)
            for turn in pending:
                try:
                    kept = _classify_turn(work, turn, config)
                except (breaker.ServiceError, breaker.PausedError) as exc:
                    _log("core-replay", exc)
                    break
                processed.append(turn)
                result["sorted"] += 1
                result["kept"] += int(kept)
        finally:
            # shortcut: death between append and ack can replay an entry, add a journal if crash deduplication is required.
            store.ack_pending(root, processed)
    return result


def stop(work: dict, config: dict) -> str:
    root = Path(work["repo_root"])
    turn = work["turn"]
    if breaker.paused("jev"):
        store.queue_turn(root, turn, breaker.ratio())
        return ""
    deferred = False
    try:
        replay_pending(work, config)
        deferred = bool(store.pending_turns(root))
        if not deferred:
            _classify_turn(work, turn, config)
    except Exception as exc:
        _log("core-stop", exc)
        deferred = True
    if deferred:
        store.queue_turn(root, turn, breaker.ratio())
        return ""
    if not breaker.paused("jev") and not breaker.paused("compactor"):
        notes = store.read_notes(root)
        if store.estimate(notes["digest"] + notes["draft"], breaker.ratio()) >= 8000:
            compactor.compact(root, config)
    return ""


def end(work: dict, config: dict) -> str:
    if not breaker.paused("jev") and not breaker.paused("compactor"):
        compactor.compact(Path(work["repo_root"]), config, force=True)
    return ""


def resume(root: Path, config: dict, *, work: Optional[dict] = None) -> str:
    results = {}
    for service, module in (("jev", jev), ("compactor", compactor), ("cognee", cognee)):
        try:
            module.probe(config)
        except Exception as exc:
            _log("core-resume-probe", exc)
            results[service] = str(exc)
            continue
        breaker.resume(service)
        results[service] = None
        for path in (store.home() / "sessions").glob("*.json"):
            def clear(session):
                session["outage_notices"] = [name for name in session.get("outage_notices", []) if name != service]
                return session
            store.update_session_if_exists(path.stem, clear)
    if results["cognee"] is None:
        for directory in sorted((store.home() / "repos").glob("*")):
            if directory.is_dir():
                try:
                    cognee.send_outbox(directory, config)
                except Exception as exc:
                    _log("core-resume-outbox", exc)
    replay = {"sorted": 0, "kept": 0}
    if results["jev"] is None:
        captured = work if work is not None else {
            "repo_root": str(Path(root).resolve()), "event": {"host": "claude-code", "transcript_path": ""},
            "session": {}, "turn": None}
        replay = replay_pending(captured, config)
    summary = []
    for service, title in (("jev", "Jev"), ("cognee", "Cognee"), ("compactor", "compactor")):
        if results[service] is not None:
            summary.append("%s ✗ still unreachable (%s)" % (title, results[service]))
        elif service == "jev":
            summary.append("Jev ✓ · %s turns sorted, %s kept" % (replay["sorted"], replay["kept"]))
        else:
            summary.append(title + " ✓")
    return redact(" · ".join(summary))
