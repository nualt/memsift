# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Unconditional secret masking before storage, clipping and HTTP requests."""

import json
import re

_SECRET_KEY = re.compile(
    r"(?i)^(?:authorization|x-api-key|[\w-]*access[_-]?key[\w-]*|(?:.*[_-])?(?:secret|token|password|passwd|api[_-]?key))$"
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
        r"(?i)[\"']?\b(?:[\w-]*[_-])?(?:secret|token|password|passwd|api[_-]?key|x-api-key|[\w-]*access[_-]?key[\w-]*)"
        r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)",
    ),
    (
        "vendor-key",
        r"(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}",
    ),
    ("vendor-key", r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("vendor-key", r"AKIA[0-9A-Z]{16}"),
    ("vendor-key", r"AIza[0-9A-Za-z_-]{35}"),
    ("vendor-key", r"(?:glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,})"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("bcrypt", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
)
_ESCAPED_CREDENTIAL = re.compile(
    r'(?is)(?<!\\)(\\+)["\'](?:[\w-]*[_-])?(?:secret|token|password|passwd|api[_-]?key|x-api-key|[\w-]*access[_-]?key[\w-]*)'
    r'\1["\']\s*[:=]\s*\1(["\']).*?(?<!\\)\1\2'
)
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _RULES]


def redact(value):
    """Redact before truncation so a clipped private key cannot escape matching."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                from .logfiles import error
                error("redact-json", exc)
            else:
                clean = redact(parsed)
                if clean == parsed:
                    return value
                return json.dumps(clean, ensure_ascii=False) + value[len(value.rstrip()):]
        value = _ESCAPED_CREDENTIAL.sub("[redacted:credential]", value)
        for kind, pattern in _COMPILED:
            value = pattern.sub(f"[redacted:{kind}]", value)
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            clean_key = redact(key) if isinstance(key, str) else key
            if clean_key != key:
                base, number = clean_key, 2
                while clean_key in result or clean_key in value:
                    clean_key = "%s (%s)" % (base, number)
                    number += 1
            result[clean_key] = "[redacted:credential]" if _SECRET_KEY.match(str(key)) else redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
