"""Typed Jev judgments, pure memory decisions and deadline-bounded requests."""

import json
import math
import threading
import time
import urllib.error
import urllib.request

from . import breaker, store
from .vendor.logfiles import decision, error
from .vendor.redact import redact

MISSING = 0.7
ENTRY = 0.5
COGNEE = 0.8
DATASET = 0.5
USEFUL = 0.7
KNOWN = 0.5
GENERAL_RULE = 0.5
CORRECTION = 0.5
CONFIRMED = 0.7
CARD_OUTDATED = 0.7
REASON_MISSING = 0.7

PROMPT_TIMEOUT = 2.0
PASSAGE_TIMEOUT = 2.0
STOP_TIMEOUT = 30.0
PROBE_TIMEOUT = 2.0
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
STATE_LIMIT = 32000
REQUEST_LIMIT = 64000

NATURES = {
    "project": "something that holds for this project and is worth remembering: a decision and its reason, a rule for how to work here, a constraint, a business rule, a fact such as an address or an account, where the work stands",
    "lesson": "a technical problem and what fixed it or did not, useful in any project, not specific to this one",
    "nothing": "status, reaction, small talk, typo, a one-off instruction, a micro-choice already applied in the code, or something left undecided",
}
SCOPES = ["this turn", "this task", "this project", "always"]


def _noul(instructions, criteria=None):
    question = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = criteria
    return question


def prompt_questions(state):
    questions = {
        "missing": _noul("Does the agent lack information, needed to act on `prompt`, that already exists: said or decided before this conversation, and not in `compaction_summary` or `recent_exchanges`?", {
            "true": "the prompt refers to work, a decision or a preference from before this conversation, which the agent cannot see",
            "false": "the information does not exist yet (it is being decided now), or the agent already has it, or the prompt needs no memory",
        }),
    }
    for identifier in state.get("notes", {}):
        questions["entry_" + identifier] = _noul("Does `notes.%s` hold information the agent needs to act on `prompt` and does not have yet?" % identifier)
    questions["cognee"] = _noul("Is it worth searching the long-term memory?", {
        "true": "the prompt refers to past work, a decision or a preference, absent from the conversation and the notes, and the agent would act differently knowing it",
        "false": "a general question, an answer found in the code or the docs, or memory would only be a bonus without changing what the agent does",
    })
    for name in state.get("datasets", {}):
        questions["dataset_" + name] = _noul("Given its description and items, would `datasets.%s` hold what the agent needs for `prompt`?" % name)
    return questions


def passage_questions(state):
    questions = {}
    passages = state.get("passages", {})
    identifiers = passages if isinstance(passages, dict) else [p["id"] for p in passages]
    for identifier in identifiers:
        number = identifier[1:]
        questions["useful_" + number] = _noul("Would `passages.%s` change what the agent does for `prompt`?" % identifier)
        questions["known_" + number] = _noul("Is the information in `passages.%s` already in `compaction_summary` or `recent_exchanges`?" % identifier)
    return questions


def stop_questions(state):
    questions = {
        "nature": {"type": "choice", "instructions": "What kind of information does `turn` carry?", "criteria": dict(NATURES)},
        "general_rule": _noul("Does `turn` state a preference about how the agent should work in every project, not only this one?", {
            "true": "a general working habit of the user, such as asking before committing, or the form of answers",
            "false": "anything specific to this project: its stack, its client, its constraints, its decisions, its rules",
        }),
        "scope": {"type": "score", "instructions": "How long does it stay true?", "criteria": list(SCOPES)},
        "correction": _noul("Does the user correct the agent in `turn`?"),
    }
    for identifier in state.get("pending_corrections", {}):
        questions["confirmed_" + identifier] = _noul("Do `turn`, `recent_exchanges` and `recent_actions` confirm `pending_corrections.%s`: applied, and not taken back?" % identifier, {
            "true": "the correction was applied and was not taken back in the recent exchanges",
            "false": "the correction was not applied, or was taken back in a text-only exchange even if older actions appear to confirm it",
        })
    questions["known"] = _noul('Is it already in `notes` or `instruction_files`, or written this turn to a file listed under "documented in" in `project_card` (an edit in `turn.actions`)?')
    if state.get("project_card"):
        questions["card_outdated"] = _noul("Does `turn` make `project_card` outdated: a new stake, a change of phase, or a contradiction?")
    questions["reason_missing"] = _noul("Does `turn` take a structuring decision without giving its reason?", {
        "true": 'a structuring decision such as "we go with Medusa" without its reason',
        "false": 'a reason is given, or a micro-choice such as "the button in blue"',
    })
    return questions


def choose_prompt(answers, entry_ids, dataset_names):
    choice = {"entries": [], "datasets": [], "recall": False}
    if answers["missing"]["noul"] < MISSING:
        return choice
    choice["entries"] = [i for i in entry_ids if answers["entry_" + i]["noul"] >= ENTRY]
    if choice["entries"] or answers["cognee"]["noul"] < COGNEE or not dataset_names:
        return choice
    choice["datasets"] = [n for n in dataset_names if answers["dataset_" + n]["noul"] >= DATASET]
    if not choice["datasets"]:
        choice["datasets"] = [max(dataset_names, key=lambda n: answers["dataset_" + n]["noul"])]
    choice["recall"] = True
    return choice


def choose_passages(answers, passages):
    return [p for p in passages if answers["useful_" + p["id"][1:]]["noul"] >= USEFUL
            and answers["known_" + p["id"][1:]]["noul"] < KNOWN]


def choose_stop(answers, pending_ids):
    nature = answers["nature"]["choice"]
    scope_answer = answers["scope"]
    levels = sorted(scope_answer["legend"], key=int)
    level = max(levels, key=lambda i: scope_answer["probabilities"][i])
    scope = scope_answer["legend"][level]
    correction = answers["correction"]["noul"] >= CORRECTION
    keep = (nature != "nothing" and scope != "this turn"
            and answers["general_rule"]["noul"] < GENERAL_RULE
            and answers["known"]["noul"] < KNOWN)
    return {"keep": keep, "nature": nature, "correction": correction,
            "confirmed": [i for i in pending_ids if answers["confirmed_" + i]["noul"] >= CONFIRMED],
            "card_outdated": "card_outdated" in answers and answers["card_outdated"]["noul"] >= CARD_OUTDATED,
            "reason_missing": keep and nature == "project" and answers["reason_missing"]["noul"] >= REASON_MISSING}


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _fits(state, questions, ratio, model="typesafe/jev-1.13"):
    longest = max((len(_json({i: q})) for i, q in questions.items()), default=0)
    state_size = store.estimate(_json(state) + " " * longest, ratio)
    total = store.estimate(_json({"model": model, "state": state, "questions": questions}), ratio)
    return state_size <= STATE_LIMIT and total <= REQUEST_LIMIT


def _tail(value, characters):
    if isinstance(value, str):
        return value[-characters:] if characters > 0 else ""
    if isinstance(value, list):
        tail = list(value)
        while len(tail) > 1 and len(_json(tail)) > characters:
            tail.pop(0)
        if tail and len(_json(tail)) > characters:
            latest = tail[0]
            if not isinstance(latest, dict) or not isinstance(latest.get("text"), str):
                return []
            low, high = 0, len(latest["text"])
            best = []
            while low <= high:
                cap = (low + high) // 2
                candidate = [dict(latest, text=latest["text"][-cap:] if cap else "")]
                if len(_json(candidate)) <= characters:
                    best = candidate
                    low = cap + 1
                else:
                    high = cap - 1
            return best
        return tail
    raise ValueError("recent exchanges must be text or a list")


def fit_stop(state, questions, ratio):
    """Fit only the classification copy; pending source text is protected."""
    try:
        fitted = redact(state)
        if "instruction_files" in fitted:
            fitted["instruction_files"] = fitted["instruction_files"][:math.floor(3000 * ratio)]
        notes = fitted.get("notes", {})
        while notes and store.estimate(_json(notes), ratio) > 10000:
            del notes[next(iter(notes))]
        if fitted.get("pending_corrections"):
            if "recent_exchanges" in fitted:
                fitted["recent_exchanges"] = _tail(fitted["recent_exchanges"], math.floor(3000 * ratio))
        else:
            fitted.pop("recent_exchanges", None)
        if _fits(fitted, questions, ratio):
            return fitted
        turn = fitted["turn"]
        texts = [turn.get("prompt", ""), turn.get("answer", "")] + turn.get("actions", [])
        original = list(texts)
        empty = dict(turn, prompt="", answer="", actions=[""] * len(turn.get("actions", [])))
        fitted["turn"] = empty
        if not _fits(fitted, questions, ratio):
            raise breaker.ServiceError("jev", "protected Stop fields exceed Jev size limits")
        total = sum(map(len, original))
        low, high = 0, total
        best = empty
        # Search a proportional allowance including JSON escapes and clip markers.
        while low <= high:
            allowance = (low + high) // 2
            clipped = [store.clip(text, math.floor(allowance * len(text) / total), 1) for text in original]
            candidate = dict(turn, prompt=clipped[0], answer=clipped[1], actions=clipped[2:])
            fitted["turn"] = candidate
            if _fits(fitted, questions, ratio):
                best = candidate
                low = allowance + 1
            else:
                high = allowance - 1
        fitted["turn"] = best
        return fitted
    except Exception as exc:
        error("jev-fit-stop", exc, "jev")
        raise


def _number(value, low=0, high=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid Jev numeric answer")


def _distribution(probabilities, keys):
    if not isinstance(probabilities, dict) or set(probabilities) != set(keys):
        raise ValueError("invalid Jev probability keys")
    for probability in probabilities.values():
        _number(probability)
    # Provider probabilities are rounded to two decimals in the verified response.
    if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01 * len(keys)):
        raise ValueError("invalid Jev probability distribution")


def _validate(body, questions):
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise ValueError("missing Jev answers")
    answers = body["answers"]
    validated = {}
    for identifier, question in questions.items():
        answer = answers.get(identifier)
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError("missing or mistyped Jev answer: " + identifier)
        if kind == "noul":
            _number(answer.get("noul"))
        elif kind in ("choice", "score"):
            _number(answer.get("confidence"))
            criteria = question["criteria"]
            if kind == "choice":
                _distribution(answer.get("probabilities"), criteria)
                selected = answer.get("choice")
                if not isinstance(selected, str) or selected not in criteria:
                    raise ValueError("invalid Jev choice")
                if answer["probabilities"][selected] != max(answer["probabilities"].values()):
                    raise ValueError("Jev choice is not the highest probability")
            else:
                legend = {str(i): text for i, text in enumerate(criteria)}
                if answer.get("legend") != legend:
                    raise ValueError("invalid Jev score legend")
                _number(answer.get("score"), high=len(criteria) - 1)
                _distribution(answer.get("probabilities"), legend)
        else:
            raise ValueError("unknown Jev question type")
        validated[identifier] = answer
    usage = body.get("usage")
    if (not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int
            or usage["input_tokens"] <= 0):
        raise ValueError("invalid Jev input token usage")
    return validated


def _minimize(state, questions):
    if "nature" in questions:
        fields = {"turn", "recent_actions", "project_card", "instruction_files", "notes", "pending_corrections"}
        if state.get("pending_corrections"):
            fields.add("recent_exchanges")
    elif any(i.startswith("useful_") for i in questions):
        fields = {"prompt", "compaction_summary", "recent_exchanges", "passages"}
    elif "missing" in questions:
        fields = {"prompt", "session_start", "project_card", "compaction_summary", "recent_exchanges", "notes", "datasets"}
    else:
        fields = set(state)
    return redact({k: v for k, v in state.items() if k in fields})


def _attempt(request, deadline):
    """Discard late results; the worker never mutates service state or calibration."""
    completed = threading.Event()
    result = {}

    def send():
        start = time.monotonic()
        try:
            remaining = deadline - start
            if remaining <= 0:
                raise TimeoutError("Jev deadline expired")
            http_error = None
            try:
                response = urllib.request.urlopen(request, timeout=remaining)
            except urllib.error.HTTPError as exc:
                error("jev-http", exc, "jev", exc.code, time.monotonic() - start)
                http_error = exc
                response = exc
            with response:
                result["status"] = response.status
                chunks = []
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Jev body deadline expired")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                result["response"] = (response.status, b"".join(chunks).decode("utf-8"), http_error)
        except Exception as exc:
            error("jev-transport", exc, "jev", getattr(exc, "code", 0), time.monotonic() - start)
            result["error"] = exc
        finally:
            completed.set()

    threading.Thread(target=send, daemon=True).start()
    if not completed.wait(max(0, deadline - time.monotonic())):
        raise breaker.ServiceError("jev", "Jev request deadline expired", result.get("status", 0))
    if "error" in result:
        raise breaker.ServiceError("jev", str(result["error"]), result.get("status", 0)) from result["error"]
    return result["response"]


def _size_refusal(exc):
    if not isinstance(exc, breaker.ServiceError):
        return False
    if exc.status == 413:
        return True
    text = exc.response.lower()
    return exc.status in (400, 422) and any(marker in text for marker in (
        "context_length_exceeded", "context length", "too many tokens", "request too large", "state too large"))


def request(config, state, questions, *, timeout, probe=False):
    if not probe:
        breaker.require_active("jev")
    start = time.monotonic()
    deadline = start + timeout
    status, response_text = 0, ""
    try:
        ratio = breaker.ratio()
        fitted = _minimize(state, questions)
        questions = redact(questions)
        for attempt in range(2):
            try:
                status, response_text = 0, ""
                if not _fits(fitted, questions, ratio, config["MEMSIFT_JEV_MODEL"]):
                    raise breaker.ServiceError("jev", "request exceeds Jev size limits")
                payload = _json(redact({"model": config["MEMSIFT_JEV_MODEL"], "state": fitted, "questions": questions}))
                wire = urllib.request.Request(ENDPOINT, data=payload.encode("utf-8"), headers={
                    "Authorization": "Bearer " + config["OPENROUTER_API_KEY"], "Content-Type": "application/json",
                }, method="POST")
                status, response_text, cause = _attempt(wire, deadline)
                if status >= 400:
                    raise breaker.ServiceError("jev", "HTTP %s" % status, status, response_text, time.monotonic() - start) from cause
                body = json.loads(response_text)
                answers = _validate(body, questions)
                tokens = body["usage"]["input_tokens"]
                decision("jev-tokens", {"estimated_tokens": store.estimate(payload, ratio), "input_tokens": tokens})
                if not probe:
                    breaker.update_ratio(len(payload), tokens)
                    breaker.record("jev", {"kind": "success", "reason": "", "status": status})
                return answers
            except Exception as exc:
                status = getattr(exc, "status", 0) or status
                error("jev-attempt", exc, "jev", status, time.monotonic() - start, response_text)
                if attempt or probe or not _size_refusal(exc):
                    raise
                recent = fitted.get("recent_exchanges")
                if recent is not None:
                    fitted["recent_exchanges"] = _tail(recent, len(recent) // 2 if isinstance(recent, str) else len(_json(recent)) // 2)
                if "nature" in questions:
                    fitted = fit_stop(fitted, questions, ratio)
                    questions = stop_questions(fitted)
                elif "missing" in questions:
                    questions = prompt_questions(fitted)
    except Exception as exc:
        error("jev-request", exc, "jev", status, time.monotonic() - start, response_text)
        failure = breaker.ServiceError("jev", str(exc), status, response_text, time.monotonic() - start)
        if not probe:
            breaker.record("jev", breaker.classify(failure))
        raise failure from exc


def probe(config):
    request(config, {"project": "acme-shop"}, {"available": _noul("Is `project` present?")}, timeout=PROBE_TIMEOUT, probe=True)
