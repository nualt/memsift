#!/usr/bin/env python3
"""Replay external human labels against Jev after explicit cost consent."""

import argparse
import copy
import itertools
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memsift import breaker, jev, store
from memsift.vendor.env_file import load_config
from memsift.vendor.logfiles import error
from memsift.vendor.redact import redact


PRICE_DATE = "2026-10-14"
INPUT_PRICES = {"typesafe/jev-1.13": 0.042}
DEFAULT_DATASETS = {
    "memory": {
        "description": "past work, decisions and lessons of this project",
        "items": [],
    }
}


def _text(value, label):
    if not isinstance(value, str):
        raise ValueError(label + " must be a string")


def _string_map(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    for key, text in value.items():
        if not isinstance(key, str) or not key or not isinstance(text, str):
            raise ValueError(label + " must map nonblank string ids to strings")


def _conversation(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    if set(value) - {"summary", "exchanges"}:
        raise ValueError(label + " has unknown fields")
    summary = value.get("summary", "")
    exchanges = value.get("exchanges", [])
    _text(summary, label + ".summary")
    if not isinstance(exchanges, list):
        raise ValueError(label + ".exchanges must be an array")
    for index, exchange in enumerate(exchanges, 1):
        item = "%s.exchanges[%d]" % (label, index)
        if not isinstance(exchange, dict) or set(exchange) - {"role", "text", "actions"}:
            raise ValueError(item + " must be an exchange object")
        _text(exchange.get("role"), item + ".role")
        _text(exchange.get("text"), item + ".text")
        actions = exchange.get("actions", [])
        if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
            raise ValueError(item + ".actions must be an array of strings")
    return {"summary": summary, "exchanges": exchanges}


def _datasets(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    for name, dataset in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError(label + " names must be nonblank strings")
        if not isinstance(dataset, dict) or set(dataset) != {"description", "items"}:
            raise ValueError(label + "." + name + " requires description and items")
        _text(dataset["description"], label + "." + name + ".description")
        if not isinstance(dataset["items"], list) or any(not isinstance(item, str) for item in dataset["items"]):
            raise ValueError(label + "." + name + ".items must be an array of strings")


def _expected(value, notes, datasets, label):
    if value == "nothing":
        return {"kind": "nothing"}
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError(label + ".expected must be nothing, entries, or cognee")
    if "entries" in value:
        entries = value["entries"]
        if (not isinstance(entries, list) or not entries
                or any(not isinstance(identifier, str) or identifier not in notes for identifier in entries)):
            raise ValueError(label + ".expected.entries must name existing notes")
        return {"kind": "entries", "entries": entries}
    if value.get("cognee") is True and set(value) == {"cognee"}:
        if not datasets:
            raise ValueError(label + " expects Cognee but has no datasets")
        return {"kind": "cognee"}
    raise ValueError(label + ".expected must be nothing, entries, or cognee true")


def _stop_state(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ".stop must be an object")
    allowed = {"turn", "recent_actions", "project_card", "instruction_files", "notes",
               "pending_corrections", "recent_exchanges"}
    if set(value) - allowed:
        raise ValueError(label + ".stop has unknown fields")
    turn = value.get("turn")
    if not isinstance(turn, dict) or set(turn) != {"prompt", "answer", "actions"}:
        raise ValueError(label + ".stop.turn requires prompt, answer and actions")
    _text(turn["prompt"], label + ".stop.turn.prompt")
    _text(turn["answer"], label + ".stop.turn.answer")
    if not isinstance(turn["actions"], list) or any(not isinstance(item, str) for item in turn["actions"]):
        raise ValueError(label + ".stop.turn.actions must be an array of strings")
    result = {
        "turn": turn,
        "recent_actions": value.get("recent_actions", []),
        "project_card": value.get("project_card", ""),
        "instruction_files": value.get("instruction_files", ""),
        "notes": value.get("notes", {}),
        "pending_corrections": value.get("pending_corrections", {}),
    }
    for field in ("recent_actions",):
        if not isinstance(result[field], list) or any(not isinstance(item, str) for item in result[field]):
            raise ValueError(label + ".stop." + field + " must be an array of strings")
    for field in ("project_card", "instruction_files"):
        _text(result[field], label + ".stop." + field)
    for field in ("notes", "pending_corrections"):
        _string_map(result[field], label + ".stop." + field)
    if "recent_exchanges" in value:
        exchanges = value["recent_exchanges"]
        if not isinstance(exchanges, (str, list)):
            raise ValueError(label + ".stop.recent_exchanges must be a string or array")
        result["recent_exchanges"] = exchanges
    return result


def _label(value, line):
    label = "line %d" % line
    if not isinstance(value, dict):
        raise ValueError(label + " must be a JSON object")
    allowed = {"prompt", "notes", "conversation", "expected", "project_card", "language",
               "session_start", "datasets", "stop"}
    if set(value) - allowed:
        raise ValueError(label + " has unknown fields")
    for field in ("prompt", "notes", "conversation", "expected"):
        if field not in value:
            raise ValueError(label + " is missing " + field)
    _text(value["prompt"], label + ".prompt")
    _string_map(value["notes"], label + ".notes")
    conversation = _conversation(value["conversation"], label + ".conversation")
    project_card = value.get("project_card", "")
    language = value.get("language", "unspecified")
    session_start = value.get("session_start", "new session")
    for field, item in (("project_card", project_card), ("language", language),
                        ("session_start", session_start)):
        _text(item, label + "." + field)
    datasets = copy.deepcopy(value.get("datasets", DEFAULT_DATASETS))
    _datasets(datasets, label + ".datasets")
    return {
        "prompt": value["prompt"],
        "notes": value["notes"],
        "conversation": conversation,
        "project_card": project_card,
        "language": language,
        "session_start": session_start,
        "datasets": datasets,
        "expected": _expected(value["expected"], value["notes"], datasets, label),
        "stop": _stop_state(value["stop"], label) if "stop" in value else None,
    }


def _load(path):
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("labels file is empty")
    labels = []
    for line, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            raise ValueError("line %d is blank" % line)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("line %d is invalid JSON: %s" % (line, exc.msg)) from exc
        labels.append(_label(value, line))
    return labels


def _states(label):
    full = {
        "prompt": label["prompt"],
        "session_start": label["session_start"],
        "project_card": label["project_card"],
        "compaction_summary": label["conversation"]["summary"],
        "recent_exchanges": label["conversation"]["exchanges"],
        "notes": label["notes"],
        "datasets": label["datasets"],
    }
    minimal = {"prompt": label["prompt"], "project_card": label["project_card"],
               "notes": label["notes"]}
    questions = jev.prompt_questions(full)
    minimal_questions = {key: question for key, question in jev.prompt_questions(minimal).items()
                         if key == "missing" or key == "cognee" or key.startswith("entry_")}
    return full, questions, minimal, minimal_questions


def _plans(labels):
    plans = []
    for index, label in enumerate(labels):
        full, full_questions, minimal, minimal_questions = _states(label)
        plans.extend((("full", index, full, full_questions),
                      ("minimal", index, minimal, minimal_questions)))
        if label["stop"] is not None:
            questions = jev.stop_questions(label["stop"])
            alternate = copy.deepcopy(questions)
            alternate["nature"]["criteria"] = dict(reversed(list(alternate["nature"]["criteria"].items())))
            plans.extend((("stop-baseline", index, label["stop"], questions),
                          ("stop-alternate", index, label["stop"], alternate)))
    return plans


def _estimated_tokens(model, state, questions, ratio):
    payload = redact({"model": model, "state": state, "questions": questions})
    return store.estimate(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ratio)


def _choice_key(choice):
    if choice["entries"]:
        return "entries", tuple(choice["entries"])
    if choice["recall"]:
        return "cognee", ()
    return "nothing", ()


def _expected_key(expected):
    return expected["kind"], tuple(expected.get("entries", []))


def _choose(answers, entry_ids, dataset_names, missing, entry, cognee, dataset):
    if answers["missing"]["noul"] < missing:
        return {"entries": [], "datasets": [], "recall": False}
    entries = [identifier for identifier in entry_ids
               if answers["entry_" + identifier]["noul"] >= entry]
    if entries or answers["cognee"]["noul"] < cognee or not dataset_names:
        return {"entries": entries, "datasets": [], "recall": False}
    selected = [name for name in dataset_names
                if answers["dataset_" + name]["noul"] >= dataset]
    if not selected:
        selected = [max(dataset_names, key=lambda name: answers["dataset_" + name]["noul"])]
    return {"entries": [], "datasets": selected, "recall": True}


def _candidates(values, current):
    candidates = {current}
    for value in values:
        candidates.add(value)
        if value < 1:
            candidates.add(math.nextafter(value, 1.0))
    return sorted(candidates)


def _best_thresholds(results):
    observed_missing = [item["answers"]["missing"]["noul"] for item in results]
    observed_entry = [answer["noul"] for item in results for key, answer in item["answers"].items()
                      if key.startswith("entry_")]
    observed_cognee = [item["answers"]["cognee"]["noul"] for item in results]
    candidates = (_candidates(observed_missing, jev.MISSING),
                  _candidates(observed_entry, jev.ENTRY),
                  _candidates(observed_cognee, jev.COGNEE))
    current = (jev.MISSING, jev.ENTRY, jev.COGNEE)
    best = None
    for thresholds in itertools.product(*candidates):
        correct = 0
        for item in results:
            choice = _choose(item["answers"], list(item["label"]["notes"]),
                             list(item["label"]["datasets"]), *thresholds, jev.DATASET)
            correct += _choice_key(choice) == _expected_key(item["label"]["expected"])
        rank = (correct, thresholds == current,
                -sum(value != baseline for value, baseline in zip(thresholds, current)),
                -sum(abs(value - baseline) for value, baseline in zip(thresholds, current)),
                tuple(-value for value in thresholds))
        if best is None or rank > best[0]:
            best = rank, thresholds
    return best[1], best[0][0]


def _summarize(name, results):
    correct = 0
    injections = 0
    misses = 0
    recalls = 0
    languages = {}
    for item in results:
        predicted = _choice_key(item["choice"])
        expected = _expected_key(item["label"]["expected"])
        correct += predicted == expected
        injections += predicted[0] != "nothing" and expected[0] == "nothing"
        misses += predicted[0] == "nothing" and expected[0] != "nothing"
        recalls += item["choice"]["recall"]
        language = item["label"]["language"]
        values = languages.setdefault(language, [0, 0])
        values[0] += predicted == expected
        values[1] += 1
    total = len(results)
    print("%s: %d/%d correct; false injections %d; misses %d; Cognee call frequency %.1f%%" %
          (name, correct, total, injections, misses, 100 * recalls / total))
    print("%s language results: %s" %
          (name, ", ".join("%s %d/%d" % (language, *values)
                           for language, values in sorted(languages.items()))))


def _parser():
    parser = argparse.ArgumentParser(description="Replay external prompt labels against Jev.")
    parser.add_argument("labels", help="external JSONL labels file")
    parser.add_argument("--input-price", type=float, help="USD per million input tokens")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        labels = _load(args.labels)
        config = load_config()
        model = config["MEMSIFT_JEV_MODEL"]
        price = args.input_price if args.input_price is not None else INPUT_PRICES.get(model)
        if price is None:
            raise ValueError("unknown Jev pricing for %s; pass --input-price" % model)
        if not math.isfinite(price) or price < 0:
            raise ValueError("input price must be a finite nonnegative number")
        plans = _plans(labels)
        ratio = breaker.ratio()
        tokens = sum(_estimated_tokens(model, state, questions, ratio)
                     for _, _, state, questions in plans)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        error("calibrate-jev-input", exc, "jev")
        print("Calibration input error: " + str(exc), file=sys.stderr)
        return 2

    print("Jev price verified %s: %s $%.6f/M input, $0/M output" %
          (PRICE_DATE, model, price))
    print("Estimate assumes the current %.3f characters/token ratio and %d input tokens." %
          (ratio, tokens))
    print("Estimated cost: $%.6f (%d requests; dated provider prices shown above)" %
          (tokens * price / 1_000_000, len(plans)))
    try:
        consent = input("Run these model calls? [y/N] ").strip().lower()
    except EOFError as exc:
        error("calibrate-jev-consent", exc, "jev")
        return 0
    if consent not in ("y", "yes"):
        return 0

    full_results = []
    minimal_results = []
    stop_results = {}
    try:
        for kind, index, state, questions in plans:
            answers = jev.request(config, redact(state), questions, timeout=30.0, probe=True)
            label = labels[index]
            if kind in ("full", "minimal"):
                adapted = dict(answers)
                if kind == "minimal":
                    adapted.update({"dataset_" + name: {"type": "noul", "noul": 0.0}
                                    for name in label["datasets"]})
                choice = jev.choose_prompt(adapted, list(label["notes"]), list(label["datasets"]))
                result = {"label": label, "answers": adapted, "choice": choice}
                (full_results if kind == "full" else minimal_results).append(result)
            else:
                pending = list(label["stop"]["pending_corrections"])
                choice = jev.choose_stop(answers, pending)
                stop_results.setdefault(index, {})[kind] = choice
    except Exception as exc:
        error("calibrate-jev-run", exc, "jev")
        print("Calibration failed: " + str(exc), file=sys.stderr)
        return 1

    _summarize("Full", full_results)
    _summarize("Minimal", minimal_results)
    thresholds, correct = _best_thresholds(full_results)
    print("Best matching prompt thresholds: missing %.6g, entry %.6g, cognee %.6g, dataset %.6g (%d/%d correct)" %
          (*thresholds, jev.DATASET, correct, len(full_results)))
    print("Minimal dataset selection: unmeasured; zero dataset scores were supplied only for offline routing.")
    if not stop_results:
        print("Nature-choice ordering sensitivity: not run (labels lack Stop examples).")
    else:
        complete = [result for result in stop_results.values()
                    if {"stop-baseline", "stop-alternate"} <= set(result)]
        changed = sum(result["stop-baseline"]["nature"] != result["stop-alternate"]["nature"]
                      for result in complete)
        print("Nature-choice ordering sensitivity: %d/%d choices changed." % (changed, len(complete)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
