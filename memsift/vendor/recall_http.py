#!/usr/bin/env python3
# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Cognee recall transport with original errors and a wall-clock deadline."""

from __future__ import annotations

import errno
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

from .dataset_access import recall_fields
from ..transport import read_response
from .logfiles import error
from .redact import redact

DOWN = "down"
SLOW = "slow"
UNKNOWN = "unknown"

# Errnos that positively identify an absent/unroutable server.
_DOWN_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def classify_transport_exception(exc) -> str:
    """Classify a transport failure as DOWN, SLOW, or UNKNOWN.

    Unwraps ``urllib.error.URLError`` (the real cause lives in ``.reason``, and
    can be an exception *or* a plain string). Order matters below:
    ``TimeoutError`` and ``ConnectionRefusedError`` are OSError subclasses, and
    ``ssl.SSLError`` is too, so the generic errno check must come last.
    """
    if isinstance(exc, urllib.error.HTTPError):
        # The server answered; HTTP statuses are the caller's business.
        return UNKNOWN
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    if isinstance(exc, str):
        return SLOW if "timed out" in exc else UNKNOWN
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SLOW
    if isinstance(exc, socket.gaierror):
        return DOWN
    if isinstance(exc, ConnectionRefusedError):
        return DOWN
    if isinstance(exc, ssl.SSLError):
        return UNKNOWN
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _DOWN_ERRNOS:
        return DOWN
    return UNKNOWN


def coerce_top_k(value, default=5):
    n = int(str(value).strip()) if value else default
    if n <= 0:
        raise ValueError("top_k must be positive")
    return n


def coerce_scope(value, default=None):
    if not value:
        return list(default if default is not None else ["graph"])
    scope = json.loads(value) if isinstance(value, str) else value
    if not isinstance(scope, list) or not scope or not all(isinstance(item, str) for item in scope):
        raise ValueError("scope must be a nonempty list of strings")
    return scope


def coerce_code_query(value):
    if not value:
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("code_query must be an object")
    return parsed


def coerce_dataset_ids(value):
    """Normalise ``dataset_ids`` from argv (comma-separated) or a list to a clean list."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(x).strip() for x in value if str(x).strip()]


def _recall_once(
    service_url,
    api_key,
    query,
    session_id,
    scope,
    top_k,
    dataset="",
    context_profile="",
    code_query=None,
    dataset_ids="",
    *,
    opener=None,
    timeout=120.0,
):
    url = service_url.rstrip("/") + "/api/v1/recall"
    body = {
        "query": query,
        "top_k": coerce_top_k(top_k),
        "only_context": True,
        "scope": coerce_scope(scope),
    }
    parsed_code_query = coerce_code_query(code_query)
    if parsed_code_query is not None:
        body["code_query"] = parsed_code_query
    if session_id:
        body["session_id"] = session_id
    fields, _ = recall_fields(dataset, body["scope"])
    ids = coerce_dataset_ids(dataset_ids)
    if ids and body["scope"] != ["graph"]:
        ids = ids[:1]
    if ids:
        body["dataset_ids"] = ids
    else:
        body.update(fields)
    if context_profile:
        body["context_profile"] = context_profile
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(
        url, data=json.dumps(redact(body)).encode("utf-8"), headers=headers, method="POST"
    )
    raw = b""
    status = 0
    try:
        status, raw = read_response(req, timeout, opener=opener)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("invalid Cognee recall response: expected a list")
    except Exception as exc:
        exc.response = getattr(exc, "response", "") or redact(raw.decode("utf-8", "replace"))
        if not isinstance(exc, urllib.error.HTTPError):
            exc.status = status
        raise
    return data


def do_recall(
    service_url,
    api_key,
    query,
    session_id,
    scope,
    top_k,
    dataset="",
    context_profile="",
    code_query=None,
    dataset_ids="",
    *,
    opener=None,
    timeout=120.0,
):
    """Return a list or raise; a late worker can only write to its private queue."""
    deadline = time.monotonic() + timeout
    result_queue = queue.Queue()

    def collect_result():
        started = time.monotonic()
        try:
            value = _recall_once(service_url, api_key, query, session_id, scope, top_k,
                                 dataset, context_profile, code_query, dataset_ids,
                                 opener=opener, timeout=timeout)
        except Exception as exc:
            response = getattr(exc, "response", "")
            status = getattr(exc, "code", getattr(exc, "status", 0))
            exc.response = redact(response)
            exc.duration = time.monotonic() - started
            error("recall", exc, "cognee", status, exc.duration, response)
            result_queue.put((None, exc))
        else:
            result_queue.put((value, None))

    worker = threading.Thread(target=collect_result, name="memsift-recall", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive() or time.monotonic() >= deadline:
        raise TimeoutError("Cognee recall deadline exceeded")
    value, exc = result_queue.get_nowait()
    if exc is not None:
        raise exc
    return value
