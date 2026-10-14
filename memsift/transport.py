"""Authenticated service transport with no redirects and bounded response reads."""

import threading
import time
import urllib.error
import urllib.request

from .vendor.logfiles import error
from .vendor.redact import redact


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout, *, opener=None):
    for name, value in request.header_items():
        if name.lower() in ("authorization", "x-api-key") and ("\r" in value or "\n" in value):
            raise ValueError("Invalid authentication header")
    if opener is not None:
        return opener(request, timeout=timeout)
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def read_response(request, timeout, *, opener=None):
    """A late reader closes its response and cannot affect the caller's state."""
    deadline = time.monotonic() + timeout
    done = threading.Event()
    result = {}

    def read():
        started = time.monotonic()
        raw = b""
        status = 0
        try:
            cause = None
            try:
                response = open_request(request, max(0.001, deadline - time.monotonic()), opener=opener)
            except urllib.error.HTTPError as exc:
                error("service-http", exc, status=exc.code, duration=time.monotonic() - started)
                response, cause = exc, exc
            with response:
                status = response.status
                result["status"] = status
                chunks = []
                try:
                    while time.monotonic() < deadline:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    else:
                        raise TimeoutError("Service response deadline expired")
                    raw = b"".join(chunks)
                except Exception as exc:
                    error("service-body", exc, status=status, duration=time.monotonic() - started)
                    if cause is not None:
                        raise cause from exc
                    raise
            if cause is not None:
                cause.response = redact(raw.decode("utf-8", "replace"))
                raise cause
            result["value"] = (status, raw)
        except Exception as exc:
            if not isinstance(exc, urllib.error.HTTPError):
                exc.status = status
            if not getattr(exc, "response", ""):
                exc.response = redact(raw.decode("utf-8", "replace"))
            error("service-response", exc, status=status, duration=time.monotonic() - started,
                  response=exc.response)
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=read, name="memsift-response", daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        exc = TimeoutError("Service response deadline expired")
        exc.status = result.get("status", 0)
        raise exc
    if "error" in result:
        raise result["error"]
    return result["value"]
