#!/usr/bin/env python3
"""Compare compactor fidelity on one external draft after cost consent."""

import argparse
import json
import math
from pathlib import Path
import re
import sys
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memsift import breaker, compactor, store
from memsift.vendor.env_file import load_config
from memsift.vendor.logfiles import error
from memsift.vendor.redact import redact


PRICE_DATE = "2026-10-14"
MODELS = {
    "anthropic/claude-haiku-5.5": {"input": 0.10, "output": 0.50},
    "google/gemma-4-31b-it": {"input": 0.09, "output": 0.34},
}
REQUIRED_MODELS = ("anthropic/claude-haiku-5.5", "google/gemma-4-31b-it")


class _RecordingResponse:
    def __init__(self, response):
        self.response = response
        self.body = bytearray()

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def read1(self, size=-1):
        chunk = self.response.read1(size)
        self.body.extend(chunk)
        return chunk


def _notes(draft):
    entries = []
    pattern = re.compile(
        r"^- \[(d\d+)\] (\d{4}-\d{2}-\d{2}) (project|lesson)\b",
        re.MULTILINE,
    )
    for identifier, date, nature in pattern.findall(draft):
        entries.append({"id": identifier, "text": "", "date": date, "nature": nature,
                        "correction": False, "pending": False})
    return {"digest": "", "draft": redact(draft), "entries": entries, "pending": {}}


def _parser():
    parser = argparse.ArgumentParser(description="Compare two compactor models on an external draft.")
    parser.add_argument("draft", help="external draft.md path")
    return parser


def _request_with_usage(config, notes):
    opener = urllib.request.urlopen
    responses = []

    def open_recorded(*args, **kwargs):
        response = _RecordingResponse(opener(*args, **kwargs))
        responses.append(response)
        return response

    with patch("urllib.request.urlopen", side_effect=open_recorded):
        answer = compactor.request(config, notes, probe=True)
    if not responses:
        return answer, None
    usage = json.loads(bytes(responses[-1].body))["usage"]
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if type(prompt) is not int or prompt < 0 or type(completion) is not int or completion < 0:
        raise ValueError("compactor response lacks measured token usage")
    return answer, (prompt, completion)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        missing = [model for model in REQUIRED_MODELS if model not in MODELS]
        if missing:
            raise ValueError("comparative model metadata unavailable: " + ", ".join(missing))
        draft_path = Path(args.draft)
        draft = draft_path.read_text(encoding="utf-8")
        if not draft.strip():
            raise ValueError("draft is empty")
        notes = _notes(draft)
        config = load_config()
        ratio = breaker.ratio()
        input_tokens = store.estimate(compactor.INSTRUCTIONS + "\n" + notes["draft"], ratio)
        output_tokens = 3000 + store.estimate(notes["draft"], ratio)
        cost = sum((input_tokens * MODELS[model]["input"]
                    + output_tokens * MODELS[model]["output"]) / 1_000_000
                   for model in REQUIRED_MODELS)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        error("compare-compactors-input", exc, "compactor")
        print("Comparison input error: " + str(exc), file=sys.stderr)
        return 2

    for model in REQUIRED_MODELS:
        price = MODELS[model]
        print("Compactor price verified %s: %s $%.2f/M input, $%.2f/M output" %
              (PRICE_DATE, model, price["input"], price["output"]))
    print("Estimate assumes %.3f characters/token, %d input and at most %d output tokens per model." %
          (ratio, input_tokens, output_tokens))
    print("Estimated cost: $%.6f (%d requests; dated provider prices shown above)" %
          (cost, len(REQUIRED_MODELS)))
    try:
        consent = input("Run these model calls? [y/N] ").strip().lower()
    except EOFError as exc:
        error("compare-compactors-consent", exc, "compactor")
        return 0
    if consent not in ("y", "yes"):
        return 0

    try:
        for model in REQUIRED_MODELS:
            answer, usage = _request_with_usage(
                dict(config, MEMSIFT_COMPACTOR_MODEL=model), notes)
            answer = compactor.validate(answer, notes)
            digest = compactor.render_digest(draft_path.parent, answer["digest"], ratio)
            measured_output = store.estimate(json.dumps(answer, ensure_ascii=False), ratio)
            print("\n=== %s ===" % model)
            print("Validation: valid")
            if usage is None:
                print("Token use (local estimate): input %d, output %d" %
                      (input_tokens, measured_output))
            else:
                print("Measured provider token use: input %d, output %d" % usage)
            print("Digest:\n" + digest.rstrip())
            print("Packet:\n" + redact(answer["packet"]["text"]))
    except Exception as exc:
        error("compare-compactors-run", exc, "compactor")
        print("Comparison failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
