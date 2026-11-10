"""JSON Schemas for source extracts (input) and ranking files (output).

Extracts are strict about which keys must be present and lenient about nulls: an
extractor may not know a cuisine or a date, but it must say so explicitly with null.
"""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

SOURCE_TYPES = ["guide", "ranking", "award", "editorial", "official", "community"]

_NULLABLE_STR = {"type": ["string", "null"]}
_DATE = {
    "type": ["string", "null"],
    # YYYY, YYYY-MM or YYYY-MM-DD (optionally followed by a time)
    "pattern": r"^\d{4}(-\d{2}(-\d{2}.*)?)?$",
}

ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "tier"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "name_ar": _NULLABLE_STR,
        "cuisine": _NULLABLE_STR,
        "area": _NULLABLE_STR,
        "price_band": _NULLABLE_STR,
        "tier": {"type": ["string", "null"]},
        "position": {"type": ["integer", "number", "string", "null"]},
        "note": _NULLABLE_STR,
        "entry_url": _NULLABLE_STR,
        "branch": _NULLABLE_STR,
    },
    "additionalProperties": True,
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Source extract",
    "type": "object",
    "required": [
        "source_id", "source_name", "publisher", "source_type", "language",
        "url", "published_at", "retrieved_at", "coverage_note", "entries",
    ],
    "properties": {
        "source_id": {"type": "string", "pattern": r"^[a-z0-9]+(-[a-z0-9]+)*$"},
        "source_name": {"type": "string", "minLength": 1},
        "publisher": {"type": "string", "minLength": 1},
        "source_type": {"type": "string", "enum": SOURCE_TYPES},
        "language": _NULLABLE_STR,
        "url": _NULLABLE_STR,
        "published_at": _DATE,
        "retrieved_at": _DATE,
        "coverage_note": _NULLABLE_STR,
        "entries": {"type": "array", "items": ENTRY_SCHEMA},
    },
    "additionalProperties": True,
}

_MENTION = {
    "type": "object",
    "required": ["source_id", "publisher", "source_type", "tier", "position",
                 "published_at", "url", "note", "weight"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Ranking output",
    "type": "object",
    "required": ["city", "category", "month", "generated_at", "method_version",
                 "n_places", "n_sources", "sources", "places"],
    "properties": {
        "month": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
        "n_places": {"type": "integer", "minimum": 0},
        "n_sources": {"type": "integer", "minimum": 0},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_id", "source_name", "publisher", "source_type",
                             "url", "published_at", "n_entries"],
            },
        },
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "rank", "score", "confidence", "name", "name_ar",
                             "aliases", "cuisines", "area", "price_band", "lat", "lng",
                             "geocode", "branches", "awards", "mentions", "why"],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "score": {"type": "number"},
                    "confidence": {"enum": ["strong", "medium", "single"]},
                    "lat": {"type": ["number", "null"]},
                    "lng": {"type": ["number", "null"]},
                    "geocode": {"enum": ["nominatim", None]},
                    "mentions": {"type": "array", "minItems": 1, "items": _MENTION},
                },
            },
        },
    },
}


def _errors(schema: dict[str, Any], data: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path))):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{path}: {err.message}")
    return out


def validate_extract(data: Any) -> list[str]:
    """Return a list of human-readable problems (empty when the extract is valid)."""
    return _errors(EXTRACT_SCHEMA, data)


def validate_output(data: Any) -> list[str]:
    return _errors(OUTPUT_SCHEMA, data)
