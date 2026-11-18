"""JSON envelope construction.

The command contract is machine-first: every command writes **one** JSON
envelope to stdout (batch and follow-log streams write one envelope per
line). Diagnostics go to stderr. The envelope shape is published in
``papercli/agent_contract/schemas/envelope.json`` and versioned
independently of the package.

    {
      "schema_version": "1.0",
      "status": "ok" | "error",
      "data": {...} | null,
      "warnings": [ ... ]
    }

Error envelopes additionally carry::

    "error": {"code": "...", "message": "...", "retryable": bool, "details": {...}}
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from papercli.agent_contract import SCHEMA_VERSION

__all__ = [
    "ok",
    "error",
    "render",
    "emit",
    "parse_line",
    "CODE_NOT_IMPLEMENTED",
    "CODE_CONFIG_REQUIRED",
]

# Stable, published error codes.
CODE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
CODE_CONFIG_REQUIRED = "CONFIG_REQUIRED"


def ok(data: Mapping[str, Any] | None = None, warnings: list[Any] | None = None) -> dict[str, Any]:
    """Build a successful envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "data": dict(data) if data is not None else None,
        "warnings": list(warnings or []),
    }


def error(
    code: str,
    message: str,
    retryable: bool = False,
    details: Mapping[str, Any] | None = None,
    warnings: list[Any] | None = None,
) -> dict[str, Any]:
    """Build an error envelope.

    ``details`` may be empty; unknown keys are allowed so callers can attach
    structured context. Secret values must never be placed here -- pass
    redacted/masked forms only.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": dict(details or {}),
        },
        "warnings": list(warnings or []),
    }


def render(envelope: Mapping[str, Any]) -> str:
    """Serialize an envelope to a single-line JSON string (UTF-8 safe)."""
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def emit(envelope: Mapping[str, Any], *, out: Any = None) -> None:
    """Write one envelope to ``out`` (defaults to stdout)."""
    stream = out if out is not None else sys.stdout
    stream.write(render(envelope) + "\n")
    stream.flush()


def parse_line(line: str) -> dict[str, Any]:
    """Parse one envelope line from a stream.

    Batch and follow-log streams carry exactly one envelope per line.
    Raises ``ValueError`` for malformed lines so callers can distinguish
    stream corruption from content.
    """
    line = line.strip()
    if not line:
        raise ValueError("empty envelope line")
    parsed = json.loads(line)
    if not isinstance(parsed, dict) or parsed.get("status") not in ("ok", "error"):
        raise ValueError("line is not a well-formed envelope")
    return parsed
