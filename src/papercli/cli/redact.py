"""Secret redaction.

Spec section 16: no key or email values may appear in logs, errors, JSON
output, exports, or diagnostic bundles. This module provides the
process-wide value mask used by ``papers config show --redact`` and
``papers doctor``.

Values enter the mask only in memory (from the process environment at call
time). They are never persisted. Committed config files must contain
environment-variable *references*, never values.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

__all__ = [
    "MaskingRedactor",
    "mask_text",
    "mask_value",
    "collect_process_secrets",
    "redact_structure",
]


def _mask(value: str) -> str:
    """Mask a secret value for display: show nothing, or at most a short head/tail."""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}...{value[-2:]}"


def mask_value(value: str | None) -> str:
    """Return a display-safe rendering of a value (never the raw secret)."""
    if value is None:
        return ""
    return _mask(str(value))


class MaskingRedactor:
    """Replace exact secret values in text with a masked placeholder.

    The redactor keeps the raw values only in memory; masked output is safe
    to write to stdout, stderr, logs, JSON envelopes, or files.
    """

    PLACEHOLDER = "***"

    def __init__(self, values: Iterable[str] = ()):
        seen: set[str] = set()
        for v in values:
            if v:
                seen.add(str(v))
        # Replace longer values first so partial prefixes never survive.
        self._values: list[str] = sorted(seen, key=len, reverse=True)

    def mask(self, text: str) -> str:
        """Return ``text`` with every tracked secret value masked."""
        if not isinstance(text, str) or not self._values:
            return text
        out = text
        for v in self._values:
            if v in out:
                out = out.replace(v, self.PLACEHOLDER)
        return out

    def add(self, value: str) -> None:
        if value and str(value) not in self._values:
            self._values.append(str(value))
            self._values.sort(key=len, reverse=True)


def collect_process_secrets(extra: Mapping[str, str] | None = None) -> MaskingRedactor:
    """Build a redactor from the current process environment.

    Every environment variable whose name matches a credential/email pattern
    is tracked by value. This is intentionally broad: anything that looks
    secret-shaped is masked on its way out.
    """
    value_pattern = re.compile(
        r"(?i)^(.*(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|EMAIL|CREDENTIALS?)$)"
    )
    values: list[str] = []
    for name, value in os.environ.items():
        if value_pattern.match(name) and value:
            values.append(value)
    if extra:
        values.extend(str(v) for v in extra.values() if v)
    return MaskingRedactor(values)


def mask_text(text: str, redactor: MaskingRedactor | None = None) -> str:
    """Mask ``text`` with a given redactor, or a fresh process redactor."""
    if redactor is None:
        redactor = collect_process_secrets()
    return redactor.mask(text)

def redact_structure(value: Any, redactor: MaskingRedactor) -> Any:
    """Walk a JSON-able structure and mask every string value in place.

    Masking per-string BEFORE serialization guarantees that secrets whose
    values contain JSON-escaped characters (``"`` and backslash) are
    removed from the envelope in full -- their escaped rendering would
    otherwise not match a raw-value substitution on serialized text.
    """
    if isinstance(value, str):
        return redactor.mask(value)
    if isinstance(value, list):
        return [redact_structure(item, redactor) for item in value]
    if isinstance(value, dict):
        return {k: redact_structure(v, redactor) for k, v in value.items()}
    return value
