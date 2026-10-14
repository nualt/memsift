#!/usr/bin/env python3
# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Cognee multipart uploads, without polling, backend fallback or error envelopes."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid

from .dataset_access import dataset_id as parse_dataset_id
from .logfiles import error
from .redact import redact


def _multipart_body(fields, files):
    boundary = f"----cogneeRemember{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, filename, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content if isinstance(content, bytes) else content.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def text_upload_name(node_set, content):
    """Upload filename for inline remember text: ``{node_set}-{sha256[:32]}.txt``.

    128 bits of the digest: the server's own content identity is an md5, so a
    longer suffix would not make two texts any more distinct than it already
    treats them.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    return f"{node_set or 'content'}-{hashlib.sha256(data).hexdigest()[:32]}.txt"


def do_remember(
    service_url,
    api_key,
    content,
    dataset,
    node_set,
    *,
    file_path=None,
    dataset_id="",
    opener=None,
    timeout=120.0,
):
    """Upload redacted text; successful responses return ok:true, errors raise."""
    started = time.monotonic()
    raw = b""
    status = 0
    try:
        url = service_url.rstrip("/") + "/api/v1/remember"
        if file_path:
            with open(file_path, "rb") as fh:
                content = fh.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        content = redact(content).encode("utf-8")
        # Hash only when the name needs it: a --file upload keeps its basename.
        filename = os.path.basename(str(file_path).rstrip("/")) if file_path else ""
        filename = filename or text_upload_name(node_set, content)

        # The explicit UUID (shared memory's canonical dataset) wins; otherwise a
        # UUID-shaped dataset is sent as datasetId and a name as datasetName.
        ident = str(dataset_id or "").strip() or parse_dataset_id(dataset)
        fields = {"node_set": node_set, "run_in_background": "true"}
        if ident:
            fields["datasetId"] = ident
        else:
            fields["datasetName"] = dataset
        body, boundary = _multipart_body(
            redact(fields),
            [("data", redact(filename).replace("\r", "_").replace("\n", "_").replace('"', "_"), content)],
        )
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        # Always attach the key when present: cognee >=1.2.2 enforces auth on its
        # API routes even on localhost (the local key is auto-minted at bootstrap),
        # and a server running with auth disabled simply ignores the header.
        if api_key:
            headers["X-Api-Key"] = api_key

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        if opener is None:
            response = urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())
        else:
            response = opener(req, timeout=timeout)
        with response as resp:
            status = resp.status
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or "error" in data or data.get("ok", True) is not True:
            raise ValueError("invalid Cognee remember response")
        result = {"ok": True}
        for key in ("dataset_id", "pipeline_run_id", "status"):
            if data.get(key):
                result[key] = str(data[key])
        return result
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            status = exc.code
            try:
                with exc:
                    raw = exc.read()
            except Exception as read_exc:
                error("remember-response", read_exc, "cognee", status,
                      time.monotonic() - started)
        response = raw.decode("utf-8", "replace")
        exc.response = redact(response)
        if not isinstance(exc, urllib.error.HTTPError):
            exc.status = status
        exc.duration = time.monotonic() - started
        error("remember", exc, "cognee", status, exc.duration, response)
        raise
