# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Dataset UUIDs are authoritative; names are only for owned datasets."""

from __future__ import annotations

from uuid import UUID


def dataset_id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        from .logfiles import error

        error("dataset-id", exc)
        return ""


def write_fields(value):
    ident = dataset_id(value)
    return {"dataset_id": ident} if ident else {"dataset_name": value}


def recall_fields(value, scope):
    ident = dataset_id(value)
    fields = {"dataset_ids": [ident]} if ident else ({"datasets": [value]} if value else {})
    return fields, False
