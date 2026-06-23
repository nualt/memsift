#!/usr/bin/env python3
"""Server-first recall against Cognee's ``/api/v1/recall``.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``cognee-search.sh`` already works under).

Contract — what gets printed to stdout:
  * a JSON **list** on a 2xx response. An **empty list is authoritative**:
    the server searched and found nothing.
  * the sentinel ``UNREACHABLE`` on ANY non-2xx status, an error-shaped
    response body, or a connection failure. A server failure (5xx), a bad
    request (4xx), or an auth rejection is **not** an authoritative "no
    results" — the caller must fall back to the CLI and warn the user rather
    than report a false negative.

Diagnostics go to stderr so the caller can surface them.
"""
import json
import sys
import urllib.error
import urllib.request

UNREACHABLE = "UNREACHABLE"


def coerce_top_k(value, default=5):
    """Best-effort positive int; never raises (a bad value must not look like a server failure)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return n if n > 0 else default


def coerce_scope(value, default="auto"):
    """Parse the JSON scope arg; fall back to "auto" on anything malformed."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def do_recall(
    service_url,
    api_key,
    query,
    session_id,
    scope,
    top_k,
    *,
    opener=urllib.request.urlopen,
    timeout=20.0,
):
    """Query the server. Return a list of results (possibly empty) or ``UNREACHABLE``."""
    url = service_url.rstrip("/") + "/api/v1/recall"
    body = {
        "query": query,
        "top_k": coerce_top_k(top_k),
        "only_context": True,
        "scope": coerce_scope(scope),
    }
    if session_id:
        body["session_id"] = session_id
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
    except urllib.error.HTTPError as e:
        # Non-2xx (5xx outage, 4xx bad request, 401/403 auth) is a failure, NOT
        # an authoritative empty result. Surface it and signal fallback.
        sys.stderr.write(
            "[cognee-search] server HTTP %s for /api/v1/recall — not authoritative, falling back\n"
            % e.code
        )
        return UNREACHABLE
    except Exception as e:  # URLError, timeout, JSON decode, etc.
        sys.stderr.write(
            "[cognee-search] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE

    # An error-shaped 2xx body is also not a real result set.
    if isinstance(data, dict) and data.get("error"):
        sys.stderr.write(
            "[cognee-search] server returned error: %s\n" % str(data.get("error"))[:160]
        )
        return UNREACHABLE
    if isinstance(data, list):
        return data
    return [data]


def main(argv):
    # argv: service_url, api_key, query, session_id, scope, top_k
    a = list(argv) + [""] * 6
    result = do_recall(a[0], a[1], a[2], a[3], a[4], a[5])
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
