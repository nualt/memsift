# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Unconditional secret masking before storage, clipping and HTTP requests."""

import re

_SECRET_KEY = re.compile(
    r"(?i)^(?:authorization|x-api-key|(?:.*[_-])?(?:secret|token|password|passwd|api[_-]?key))$"
)
_RULES = (
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    ),
    ("connection", r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s\"'<>]+"),
    ("authorization", r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    (
        "credential",
        r"(?i)[\"']?\b(?:[\w-]*[_-])?(?:secret|token|password|passwd|api[_-]?key|x-api-key)"
        r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)",
    ),
    (
        "vendor-key",
        r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}",
    ),
    ("bcrypt", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
)
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _RULES]


def redact(value):
    """Redact before truncation so a clipped private key cannot escape matching."""
    if isinstance(value, str):
        for kind, pattern in _COMPILED:
            value = pattern.sub(f"[redacted:{kind}]", value)
        return value
    if isinstance(value, dict):
        return {
            key: "[redacted:credential]" if _SECRET_KEY.match(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
