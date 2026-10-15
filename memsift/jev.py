"""Typed Jev judgments, pure memory decisions and deadline-bounded requests."""

import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

from . import breaker, host, store
from . import transport
from .vendor.logfiles import decision, error
from .vendor.redact import redact

HAS_INFO = 0.5
ENTRY = 0.5
COGNEE = 0.8
DATASET = 0.5
PASSAGE = 0.7
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

# Keep what would save redoing the whole path if the same situation came back tomorrow.
# UI details belong in a design system, not in memory.
NATURE_QUESTION = ("What kind of information does `turn` carry? Keep only what would save redoing "
                   "the whole path if the same situation came back tomorrow.")
NATURES = {
    "project": {
        "what": "a technical choice for this project and its reason (stack, provider, hosting, architecture), a constraint of this project that causes or will cause problems, or a structured feedback on how something went",
        "examples": ["we pick Postgres over MongoDB because orders need transactions",
                     "the client's host only allows PHP 7, so no recent framework"],
    },
    "lesson": {
        "what": "a technical problem met while working and what fixed it or did not, worth knowing if it happens again",
        "not_for": "a general-knowledge question answered with no problem met",
        "examples": ["the payment webhook failed because the body was parsed before the signature check; reading it raw fixed it"],
    },
    "nothing": {
        "what": "a UI detail (layout, grid, order, colour, placement, spacing: it belongs in a design system), a task done once, small talk, status, a general-knowledge question, or something left undecided",
        "examples": ["make the buttons rounded", "move the search bar to the right", "ok, commit"],
    },
}
SCOPES = ["this turn", "this task", "this project", "always"]


def _noul(instructions, criteria=None):
    question = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = criteria
    return question


def prompt_questions(state):
    questions = {
        "has_info": _noul("Does `conversation` already contain everything the agent needs to act on `prompt`?", {
            "true": "the conversation states it, or the prompt needs nothing from the past: ok, commit, a new instruction, a general knowledge question (\"what is a webhook?\")",
            "false": "the prompt relies on a past choice, rule or fact of this project (\"prices excluding or including tax?\") that the conversation does not state",
        }),
    }
    for identifier, text in state.get("notes", {}).items():
        questions["entry_" + identifier] = _noul({
            "note": text,
            "question": "Does `note` answer `prompt` or give information needed to act on it?",
        }, {
            "true": "the note is about what the prompt asks or needs",
            "false": "the note is about something else, or the prompt needs nothing from it",
        })
    # Asks for the kind of topic the Stop judgment keeps, not whether the agent lacks the
    # information: Jev cannot see that, but it sorts topics well.
    questions["cognee"] = _noul("Does `prompt` touch a topic where a past choice or problem of this project would change what the agent does?", {
        "true": {"what": "a technical choice and its reason (stack, provider, hosting, architecture, database), a constraint of this project, a problem met before or coming back",
                 "examples": ["can we switch the emails to another provider?", "the deploy broke again like last time"]},
        "false": {"what": "a UI detail (layout, colour, placement), a reaction to the last message, a task that carries everything it needs, a general-knowledge question",
                  "examples": ["move the logo to the left", "rename the contact page", "what does HTTP 418 mean?"]},
    })
    for name in state.get("datasets", {}):
        questions["dataset_" + name] = _noul("Given its description and items, would `datasets.%s` hold what the agent needs for `prompt`?" % name)
    return questions


def prompt_requests(state):
    """Build the three isolated judgments shared by the hook and calibration."""
    questions = prompt_questions(state)
    return [
        ({"prompt": state["prompt"], "conversation": state.get("conversation", [])},
         {"has_info": questions["has_info"]}),
        ({"prompt": state["prompt"]},
         {key: value for key, value in questions.items() if key.startswith("entry_")}),
        ({"prompt": state["prompt"], "datasets": state.get("datasets", {})},
         {key: value for key, value in questions.items() if key == "cognee" or key.startswith("dataset_")}),
    ]


def request_prompt(config, state, *, timeout=PROMPT_TIMEOUT, probe=False):
    """Run all three requests under one deadline; discard incomplete judgments."""
    if not probe:
        breaker.require_active("jev")
    deadline = time.monotonic() + timeout
    try:
        ratio = breaker.ratio()
        plans = [(host.fit_prompt(fields, questions, ratio), questions)
                 for fields, questions in prompt_requests(state)]
        completed = [threading.Event() for _ in plans]
        results = [{} for _ in plans]
        failures = []

        def send(index):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Jev prompt deadline expired")
                fields, questions = plans[index]
                results[index] = _request(config, fields, questions, timeout=remaining, probe=probe, record=False)
            except Exception as exc:
                error("jev-prompt-part", exc, "jev")
                failures.append(exc)
            finally:
                completed[index].set()

        for index in range(len(plans)):
            threading.Thread(target=send, args=(index,), daemon=True).start()
        for ready in completed:
            if not ready.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("Jev prompt deadline expired")
        if failures:
            raise failures[0]
        if not probe:
            breaker.record("jev", {"kind": "success", "reason": "", "status": 200})
        return {key: value for result in results for key, value in result.items()}
    except Exception as exc:
        error("jev-prompt", exc, "jev")
        if not probe and not isinstance(exc, breaker.PausedError):
            breaker.record("jev", breaker.classify(exc))
        raise


def passage_questions(state):
    passages = state.get("passages", {})
    if not isinstance(passages, dict):
        passages = {passage["id"]: passage["text"] for passage in passages}
    return {"passage_" + identifier[1:]: _noul({
        "passage": text,
        "question": "Does `passage` answer `prompt` or give information needed to act on it?",
    }, {
        "true": "the passage is about what the prompt asks or needs",
        "false": "the passage is about something else, or the prompt needs nothing from it",
    }) for identifier, text in passages.items()}


def stop_questions(state):
    questions = {
        "nature": {"type": "choice", "instructions": NATURE_QUESTION, "criteria": dict(NATURES)},
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
    if answers["has_info"]["noul"] >= HAS_INFO:
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
    return [p for p in passages if answers["passage_" + p["id"][1:]]["noul"] >= PASSAGE]


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
        def age(identifier):
            entries = store._entries(notes[identifier])
            if not entries:
                return ""
            stamp = re.search(r" · timestamp (\S+)", notes[identifier].split("\n", 1)[0])
            return stamp.group(1) if stamp else entries[0]["date"]
        while notes and store.estimate(_json(notes), ratio) > 10000:
            del notes[min(notes, key=age)]
        if fitted.get("pending_corrections"):
            if "recent_exchanges" in fitted:
                fitted["recent_exchanges"] = _tail(fitted["recent_exchanges"], math.floor(3000 * ratio))
        else:
            fitted.pop("recent_exchanges", None)
        if _fits(fitted, questions, ratio):
            return fitted
        turn = fitted["turn"]
        actions = turn.get("actions", [])
        recent = fitted.get("recent_actions", [])
        original = [turn.get("prompt", ""), turn.get("answer", "")] + actions + recent
        empty = dict(turn, prompt="", answer="", actions=[""] * len(turn.get("actions", [])))
        fitted["turn"] = empty
        if "recent_actions" in fitted:
            fitted["recent_actions"] = [""] * len(recent)
        if not _fits(fitted, questions, ratio):
            raise breaker.ServiceError("jev", "protected Stop fields exceed Jev size limits")
        total = sum(map(len, original))
        low, high = 0, total
        best = empty
        best_recent = fitted.get("recent_actions", [])
        # Search a proportional allowance including JSON escapes and clip markers.
        while low <= high:
            allowance = (low + high) // 2
            clipped = [store.clip(text, math.floor(allowance * len(text) / total), 1) for text in original]
            candidate = dict(turn, prompt=clipped[0], answer=clipped[1], actions=clipped[2:2 + len(actions)])
            fitted["turn"] = candidate
            if "recent_actions" in fitted:
                fitted["recent_actions"] = clipped[2 + len(actions):]
            if _fits(fitted, questions, ratio):
                best = candidate
                best_recent = fitted.get("recent_actions", [])
                low = allowance + 1
            else:
                high = allowance - 1
        fitted["turn"] = best
        if "recent_actions" in fitted:
            fitted["recent_actions"] = best_recent
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
    elif "has_info" in questions:
        fields = {"prompt", "conversation"}
    elif "cognee" in questions or any(i.startswith("dataset_") for i in questions):
        fields = {"prompt", "datasets"}
    elif any(i.startswith(("entry_", "passage_")) for i in questions):
        fields = {"prompt"}
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
                response = transport.open_request(request, timeout=remaining)
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
    return _request(config, state, questions, timeout=timeout, probe=probe)


def _request(config, state, questions, *, timeout, probe=False, record=True):
    if not probe:
        breaker.require_active("jev")
    if not questions:
        # Jev refuses a request without questions (HTTP 400, "At least one question
        # is required"): a repo with no notes yet must not count that as a failure.
        return {}
    start = time.monotonic()
    deadline = start + timeout
    status, response_text = 0, ""
    try:
        ratio = breaker.ratio()
        wire_state = dict(state)
        wire_questions = dict(questions)
        answer_ids = {}
        datasets = state.get("datasets", {})
        names = {name: name for name in datasets}
        for index, name in enumerate(datasets):
            if redact(name) == name:
                continue
            alias = "dataset%d" % (index + 1)
            while alias in names.values():
                alias += "_"
            names[name] = alias
            identifier = "dataset_" + name
            if identifier in wire_questions:
                question = dict(wire_questions.pop(identifier))
                question["instructions"] = question["instructions"].replace("datasets." + name, "datasets." + alias)
                wire_identifier = "dataset_" + alias
                wire_questions[wire_identifier] = question
                answer_ids[wire_identifier] = identifier
        if datasets:
            wire_state["datasets"] = {names[name]: value for name, value in datasets.items()}
        fitted = _minimize(wire_state, wire_questions)
        questions = redact(wire_questions)
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
                    if record:
                        breaker.record("jev", {"kind": "success", "reason": "", "status": status})
                return {answer_ids.get(identifier, identifier): value for identifier, value in answers.items()}
            except Exception as exc:
                status = getattr(exc, "status", 0) or status
                error("jev-attempt", exc, "jev", status, time.monotonic() - start, response_text)
                if attempt or probe or not _size_refusal(exc):
                    raise
                if "has_info" in questions:
                    recent = fitted.get("conversation", [])
                    count = len(recent) // 2
                    fitted["conversation"] = recent[-count:] if count else []
                elif "nature" in questions:
                    recent = fitted.get("recent_exchanges")
                    if recent is not None:
                        fitted["recent_exchanges"] = _tail(recent, len(recent) // 2 if isinstance(recent, str) else len(_json(recent)) // 2)
                    fitted = fit_stop(fitted, questions, ratio)
                    questions = stop_questions(fitted)
    except Exception as exc:
        error("jev-request", exc, "jev", status, time.monotonic() - start, response_text)
        failure = breaker.ServiceError("jev", str(exc), status, response_text, time.monotonic() - start)
        if not probe and record:
            breaker.record("jev", breaker.classify(failure))
        raise failure from exc


def probe(config):
    request(config, {"project": "acme-shop"}, {"available": _noul("Is `project` present?")}, timeout=PROBE_TIMEOUT, probe=True)
