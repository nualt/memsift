"""Scoped Cognee recall, bounded dataset indexes and durable packet uploads."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import breaker, store
from . import transport
from .vendor import recall_http, recall_text, remember_http
from .vendor.file_lock import file_lock
from .vendor.logfiles import decision, error
from .vendor.redact import redact

UPLOAD_TIMEOUT = 12.0
INDEX_TOKENS = 1000
LOCK_WAIT = 1.0


def descriptions():
    result = {}
    for line in store.read_text(store.home() / "datasets.md").splitlines():
        if line.startswith("- ") and ":" in line:
            name, description = line[2:].split(":", 1)
            if name.strip():
                result[name.strip()] = description.strip()
    return result


def _string(value, field, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip()) or
            any(ord(char) < 32 for char in value)):
        raise ValueError("Invalid Cognee " + field)
    return value


def _datasets(value):
    if not isinstance(value, list):
        raise ValueError("Cognee datasets must be a list")
    rows = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Cognee dataset must be an object")
        rows.append({"id": _string(item.get("id"), "dataset ID"),
                     "name": _string(item.get("name"), "dataset name"), "items": []})
    return rows


def _items(value):
    if not isinstance(value, list):
        raise ValueError("Cognee items must be a list")
    names = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Cognee item must be an object")
        names.append(_string(item.get("name"), "item name"))
    return names


def _failure(event, exc, started, status=0, response=""):
    status = getattr(exc, "code", 0) or getattr(exc, "status", 0) or status
    duration = time.monotonic() - started
    response = getattr(exc, "response", "") or response
    error(event, exc, "cognee", status, duration, response)
    return breaker.ServiceError("cognee", str(getattr(exc, "reason", exc)),
                                status, response, duration)


def _get(config, path, validate, *, probe=False):
    if not probe:
        breaker.require_active("cognee")
    started, status, raw = time.monotonic(), 0, b""
    try:
        request = urllib.request.Request(config["COGNEE_BASE_URL"].rstrip("/") + path,
                                         headers={"X-Api-Key": config["COGNEE_API_KEY"]})
        status, raw = transport.read_response(request, UPLOAD_TIMEOUT)
        result = validate(json.loads(raw.decode("utf-8")))
    except Exception as exc:
        failure = _failure("cognee-get", exc, started, status, raw.decode("utf-8", "replace"))
        if not probe:
            breaker.record("cognee", breaker.classify(failure))
        raise failure from exc
    if not probe:
        breaker.record("cognee", {"kind": "success", "reason": "", "status": status})
    return redact(result)


def list_datasets(config, *, probe=False):
    return _get(config, "/api/v1/datasets", _datasets, probe=probe)


def _serialized(index):
    return json.dumps(redact(index), ensure_ascii=False, separators=(",", ":")) + "\n"


def _fit_index(index, own_name, ratio):
    original = [len(dataset["items"]) for dataset in index["datasets"]]
    own = [dataset for dataset in index["datasets"] if dataset["name"] == own_name]
    while store.estimate(_serialized(index), ratio) > INDEX_TOKENS and any(d["items"] for d in own):
        # With duplicate server names, remove the oldest packet across their lists.
        oldest = min((d for d in own if d["items"]), key=lambda d: _packet_order(d["items"][-1]))
        oldest["items"].pop()
    if store.estimate(_serialized(index), ratio) > INDEX_TOKENS:
        others = [d for d in index["datasets"] if d["name"] != own_name]
        full = [d["items"] for d in others]
        for dataset in others:
            dataset["items"] = []
        base_size = len(_serialized(index))
        if store.estimate(_serialized(index), ratio) > INDEX_TOKENS:
            raise ValueError("Cognee index mandatory metadata exceeds 1000 tokens")
        share = max(0, int(INDEX_TOKENS * ratio) - base_size) // max(1, len(others))
        for dataset, items in zip(others, full):
            used = 0
            for item in items:
                cost = len(json.dumps(item, ensure_ascii=False)) + (1 if dataset["items"] else 0)
                if used + cost > share:
                    break
                dataset["items"].append(item)
                used += cost
        # Recheck the serialized cache, including commas, UTF-8 text and metadata.
        while store.estimate(_serialized(index), ratio) > INDEX_TOKENS:
            largest = max((d for d in others if d["items"]),
                          key=lambda d: len(json.dumps(d["items"], ensure_ascii=False)))
            largest["items"].pop()
    remaining = [len(dataset["items"]) for dataset in index["datasets"]]
    if original != remaining:
        decision("cognee-index-cut", {"before": original, "after": remaining})


def _packet_order(name):
    stamp = re.search(r" (\d{4}-\d\d-\d\d) (\d\dh\d\d) ", name)
    stem = name[:-4] if name.endswith(".txt") else name
    suffix = re.search(r" \((\d+)\)$", stem)
    base = stem[:suffix.start()] if suffix else stem
    number = int(suffix.group(1)) if suffix else 1
    return ((stamp.group(1), stamp.group(2)) if stamp else ("", "")) + (base, number, name)


def rebuild_index(root, config):
    described = descriptions()
    rows = [row for row in list_datasets(config) if described.get(row["name"]) != "IGNORED"]
    undescribed = [row["name"] for row in rows if row["name"] not in described]
    own_name = config["MEMSIFT_DATASET"]
    prefix = redact(Path(root).name) + " "
    for row in rows:
        ident = urllib.parse.quote(row["id"], safe="")
        row["items"] = _get(config, "/api/v1/datasets/" + ident + "/data", _items)
        if row["name"] == own_name:
            row["items"] = sorted((name for name in row["items"] if name.startswith(prefix)),
                                  key=_packet_order, reverse=True)
    index = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "datasets": rows}
    started = time.monotonic()
    try:
        _fit_index(index, own_name, breaker.ratio())
        store.write_text(store.repo_dir(root) / "cognee-index.json", _serialized(index))
    except Exception as exc:
        raise _failure("cognee-index", exc, started) from exc
    return list(dict.fromkeys(undescribed))


def _passages(value, prompt):
    passages = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("text")
        else:
            text = item
        if not isinstance(text, str):
            raise ValueError("Invalid Cognee passage text")
        context = recall_text.split_only_context(text, prompt)["context"]
        sections = recall_text.split_sections(context)
        block = sections["passages"] if sections["passages"] is not None else sections["preamble"]
        passages.extend(redact(p.strip()) for p in recall_text.split_passages(block))
    return passages


def recall(config, prompt, datasets, *, timeout=12.0):
    if not datasets:
        return []
    ignored = descriptions()
    selected = [dataset for dataset in datasets if ignored.get(dataset.get("name")) != "IGNORED"]
    if not selected:
        return []
    breaker.require_active("cognee")
    started = time.monotonic()
    deadline = started + min(12.0, timeout)
    results = queue.Queue()

    def collect(position, dataset):
        began = time.monotonic()
        response = ""
        try:
            name = _string(dataset.get("name"), "dataset name")
            ident = _string(dataset.get("id", ""), "dataset ID", empty=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Cognee recall deadline exceeded")
            data = recall_http.do_recall(config["COGNEE_BASE_URL"], config["COGNEE_API_KEY"],
                                         redact(prompt), "", ["graph"], 5, dataset=name,
                                         dataset_ids=[ident] if ident else "",
                                         timeout=remaining)
            response = json.dumps(data, ensure_ascii=False)
            results.put((position, _passages(data, redact(prompt)), None))
        except Exception as exc:
            status = getattr(exc, "code", 0) or getattr(exc, "status", 0)
            if response:
                exc.response, exc.status = response, 200
            error("cognee-recall-worker", exc, "cognee", status or getattr(exc, "status", 0),
                  time.monotonic() - began, getattr(exc, "response", ""))
            empty = status == 404 and any(marker in getattr(exc, "response", "").lower()
                                          for marker in ("datasetnotfounderror", "dataset not found", "no graph"))
            results.put((position, [] if empty else None, None if empty else exc))

    try:
        # Upstream graph results carry source/text, without per-passage dataset provenance.
        for position, dataset in enumerate(selected):
            threading.Thread(target=collect, args=(position, dataset),
                             name="memsift-recall-dataset", daemon=True).start()
        collected = [None] * len(selected)
        for _ in selected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Cognee recall deadline exceeded")
            try:
                position, texts, exc = results.get(timeout=remaining)
            except queue.Empty as exc:
                error("cognee-recall-deadline", exc, "cognee", duration=time.monotonic() - started)
                raise TimeoutError("Cognee recall deadline exceeded") from exc
            if exc is not None:
                raise exc
            collected[position] = texts
        if time.monotonic() >= deadline:
            raise TimeoutError("Cognee recall deadline exceeded")
        passages = []
        depth = 0
        while len(passages) < 5 and any(len(texts) > depth for texts in collected):
            for dataset, texts in zip(selected, collected):
                if len(texts) > depth:
                    passages.append({"id": "p" + str(len(passages) + 1),
                                     "dataset": redact(dataset["name"]), "text": texts[depth]})
                    if len(passages) == 5:
                        break
            depth += 1
    except Exception as exc:
        failure = _failure("cognee-recall", exc, started)
        breaker.record("cognee", breaker.classify(failure))
        raise failure from exc
    breaker.record("cognee", {"kind": "success", "reason": "", "status": 200})
    return passages


def _packet_name(name):
    _string(name, "packet name")
    if not name.endswith(".txt") or any(char in name for char in ('/', '\\', '"', '\x7f')):
        raise ValueError("Invalid Cognee packet filename")
    return redact(name[:-4]) + ".txt"


def _available(directory, name):
    path = directory / name
    suffix = re.search(r" \((\d+)\)$", name[:-4])
    stem = name[:-4][:suffix.start()] if suffix else name[:-4]
    number = int(suffix.group(1)) + 1 if suffix else 2
    while path.exists() or path.is_symlink():
        path = directory / (stem + " (" + str(number) + ").txt")
        number += 1
    return path


def queue_packet(root, name, text):
    started = time.monotonic()
    try:
        name = _packet_name(name)
        text = redact(text).replace("\r\n", "\n").replace("\r", "\n")
        directory = store.repo_dir(root) / "outbox"
        with file_lock(directory / "allocation.lock", timeout=LOCK_WAIT) as acquired:
            if not acquired:
                raise TimeoutError("Cognee packet allocation lock unavailable")
            path = _available(directory, name)
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                if output.write(text) != len(text):
                    raise OSError("Incomplete Cognee packet write")
                output.flush()
                os.fsync(output.fileno())
            # Publish complete bytes exclusively; a drain cannot see a partial packet.
            os.link(temporary, path)
            temporary.unlink()
        return path
    except Exception as exc:
        error("cognee-queue", exc, duration=time.monotonic() - started)
        raise


def _rename_collision(packet, stack):
    with file_lock(packet.parent / "allocation.lock", timeout=LOCK_WAIT) as acquired:
        if not acquired:
            raise TimeoutError("Cognee packet allocation lock unavailable")
        destination = _available(packet.parent, packet.name)
        acquired = stack.enter_context(file_lock(destination.with_name(destination.name + ".send.lock")))
        if not acquired:
            raise TimeoutError("Cognee packet rename lock unavailable")
        os.rename(packet, destination)
        return destination


def send_outbox(repo_dir, config):
    directory = Path(repo_dir) / "outbox"
    if not directory.exists():
        return 0
    sent = 0
    started = time.monotonic()
    try:
        for packet in sorted(directory.glob("*.txt"), key=lambda p: _packet_order(p.name)):
            breaker.require_active("cognee")
            with ExitStack() as stack:
                acquired = stack.enter_context(file_lock(packet.with_name(packet.name + ".send.lock")))
                if not acquired:
                    return sent
                if not packet.exists():
                    return sent
                _packet_name(packet.name)
                if packet.is_symlink():
                    raise ValueError("Cognee outbox packet must not be a symlink")
                store.write_text(packet, store.read_text(packet))
                while True:
                    breaker.require_active("cognee")
                    attempt = time.monotonic()
                    try:
                        result = remember_http.do_remember(
                            config["COGNEE_BASE_URL"], config["COGNEE_API_KEY"], "",
                            config["MEMSIFT_DATASET"], config["MEMSIFT_DATASET"],
                            file_path=packet, timeout=UPLOAD_TIMEOUT)
                        if not isinstance(result, dict) or result.get("ok") is not True:
                            raise ValueError("Cognee did not accept packet")
                    except Exception as exc:
                        failure = _failure("cognee-upload", exc, attempt)
                        breaker.record("cognee", breaker.classify(failure))
                        if failure.status != 409:
                            return sent
                        packet = _rename_collision(packet, stack)
                        continue
                    breaker.record("cognee", {"kind": "success", "reason": "", "status": 200})
                    packet.unlink()
                    sent += 1
                    break
    except Exception as exc:
        error("cognee-outbox", exc, "cognee", duration=time.monotonic() - started)
    return sent


def probe(config):
    list_datasets(config, probe=True)
