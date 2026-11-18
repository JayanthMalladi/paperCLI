"""Loading and validating the published JSON Schemas.

Schemas live in ``papercli/agent_contract/schemas/`` and are versioned
independently of the package (``schema_version`` is currently ``"1.0"``).
``papers schema`` prints and validates against these.

Validation uses the stdlib where possible; when the optional
``jsonschema`` package is available it is used for full draft-2020-12
validation. A light structural check always runs so the command works even
without the optional dependency installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_VERSION = "1.0"
SCHEMAS_DIR = Path(__file__).resolve().parent

# Canonical schema names published to agents.
_PUBLISHED = [
    "envelope",
    "brief",
    "plan",
    "tasks",
    "screening",
    "evidence",
    "claims",
    "bibliography",
    "run_summary",
]

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMAS_DIR",
    "schema_names",
    "load_schema",
    "list_schemas",
    "validate",
    "validate_envelope",
    "PUBLISHED_SCHEMAS",
]

PUBLISHED_SCHEMAS = list(_PUBLISHED)


def schema_names() -> list[str]:
    """All schema names on disk (published + any future additions)."""
    names = []
    for p in sorted(SCHEMAS_DIR.glob("*.json")):
        names.append(p.stem)
    return names


def load_schema(name: str) -> dict:
    """Load one schema document by name."""
    if name not in schema_names():
        raise KeyError(f"unknown schema: {name!r}")
    p = SCHEMAS_DIR / f"{name}.json"
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_schemas() -> dict[str, dict]:
    """Return {name: {"title":..., "required":[...], "id":...}} for all schemas."""
    out: dict[str, dict] = {}
    for name in schema_names():
        doc = load_schema(name)
        out[name] = {
            "name": name,
            "title": doc.get("title"),
            "$id": doc.get("$id"),
            "required": list(doc.get("required", [])),
        }
    return out


def _jsonschema_validate(instance: dict, schema: dict) -> list[str]:
    """Full draft-2020-12 validation via the optional jsonschema package."""
    import jsonschema
    from jsonschema.validators import Draft202012Validator
    validator = Draft202012Validator(schema)
    return [
        f"{list(e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]


def _structural_validate(instance: dict, schema: dict) -> list[str]:
    """Lightweight validation of required keys and top-level type.

    Runs even without the optional jsonschema dependency, so ``papers schema``
    always works on a bare install.
    """
    errors: list[str] = []
    if not isinstance(instance, dict):
        errors.append("instance must be a JSON object")
        return errors
    for req in schema.get("required", []):
        if req not in instance:
            errors.append(f"missing required key: {req!r}")
    t = schema.get("type")
    if isinstance(t, str) and t == "object" and not isinstance(instance, dict):
        errors.append("instance must be an object")
    return errors


def validate(instance: dict, schema_name: str, *, full: bool = True) -> list[str]:
    """Validate ``instance`` against ``schema_name``; return a list of errors.

    An empty list means the instance is valid. With ``full=False`` only the
    structural (required-keys) check runs.
    """
    schema = load_schema(schema_name)
    errors = _structural_validate(instance, schema)
    if full:
        try:
            errors.extend(_jsonschema_validate(instance, schema))
        except ModuleNotFoundError:
            # Optional dependency absent; the structural check above already ran.
            pass
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def validate_envelope(envelope: dict, *, full: bool = True) -> list[str]:
    """Validate a JSON envelope against the published envelope schema."""
    return validate(envelope, "envelope", full=full)
