"""Identifier normalization: DOIs and arXiv ids (Phase 2).

Rules follow the verified provider facts in ``research/provider-docs``:

* DOIs are case-insensitive; the canonical form is lowercase. A valid DOI
  is ``10.NNNN/suffix`` (registrant code of 4-9 digits, non-empty suffix).
  Normalization strips transport wrappers (``doi:``, ``https://doi.org/``,
  ``dx.doi.org``) and surrounding whitespace but **never strips valid
  suffix punctuation** (colons, slashes, trailing dots, etc. may be part
  of the suffix).
* arXiv ids come in two shapes: new-style ``NNNN.NNNNN`` and legacy
  ``archive/category/NNNNNNN``. A ``vN`` version suffix is preserved as
  separate identity (spec section 8: preserve version identity).
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_doi",
    "parse_identifier",
    "normalize_arxiv_id",
    "doi_matches",
    "arxiv_pdf_url",
    "arxiv_abs_url",
]

_DOI_URL_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "https://arxiv.org/doi/",
    "http://arxiv.org/doi/",
    "doi:",
)

# 10.<4-9 digits>/suffix; suffix is any non-whitespace run, punctuation included.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# New-style: 1605.0000 [vN]
_ARXIV_NEW_RE = re.compile(r"^(\d{4}\.\d{4,5})(v(\d+))?$")
# Legacy: archive[/subclass]/NNNNNNN [vN]  (e.g. cond-mat/0207270v3, math.GT/0309136)
_ARXIV_LEGACY_RE = re.compile(
    r"^((?:[a-z][a-z.\-]*(?:/[a-z][a-z.\-]*)?)/(\d{7}|\d{4}\.\d{4,5}))(v(\d+))?$"
)


def normalize_doi(raw: str) -> str:
    """Normalize a DOI to its canonical lowercase form.

    Strips URL/``doi:`` wrappers and whitespace. Valid suffix punctuation
    is kept verbatim (it may be part of the DOI). Raises ``ValueError``
    when the input is not a well-formed DOI.
    """
    s = str(raw).strip()
    low = s.lower()
    for pref in _DOI_URL_PREFIXES:
        if low.startswith(pref):
            s = s[len(pref):]
            break
    s = s.strip().lower()
    if not _DOI_RE.match(s):
        raise ValueError(f"not a valid DOI: {raw!r}")
    return s


def doi_matches(requested: str, returned: str | None) -> bool:
    """Case-insensitive DOI comparison after normalization."""
    if returned is None:
        return False
    try:
        return normalize_doi(requested) == normalize_doi(returned)
    except ValueError:
        return str(requested).strip().lower() == str(returned).strip().lower()


def normalize_arxiv_id(raw: str) -> tuple[str, str | None]:
    """Normalize an arXiv id to ``(base_id, version)``.

    ``version`` is ``"v3"``-style when a version suffix is present, else
    ``None`` (meaning "latest"). Accepts bare ids, ``arXiv:``-prefixed ids,
    and abs/pdf URLs. Raises ``ValueError`` on anything unrecognizable.
    """
    s = str(raw).strip().lower()
    for pref in (
        "https://arxiv.org/abs/",
        "http://arxiv.org/abs/",
        "https://arxiv.org/pdf/",
        "http://arxiv.org/pdf/",
        "https://arxiv.org/",
        "http://arxiv.org/",
        "arxiv.org/abs/",
        "arxiv.org/pdf/",
        "arxiv:",
        "http://export.arxiv.org/abs/",
        "https://export.arxiv.org/abs/",
    ):
        if s.startswith(pref):
            s = s[len(pref):]
            break
    s = s.strip().rstrip("/")
    m = _ARXIV_NEW_RE.match(s)
    if m:
        return m.group(1), (f"v{m.group(3)}" if m.group(3) else None)
    m = _ARXIV_LEGACY_RE.match(s)
    if m:
        # group(1)=full base id, group(4)=version number
        return m.group(1), (f"v{m.group(4)}" if m.group(4) else None)
    raise ValueError(f"not a recognizable arXiv id: {raw!r}")


def arxiv_pdf_url(base_id: str, version: str | None = None) -> str:
    """Full-text PDF URL; version identity is preserved in the URL."""
    return f"https://arxiv.org/pdf/{base_id}{version or ''}"


def arxiv_abs_url(base_id: str, version: str | None = None) -> str:
    """Landing-page URL; version identity is preserved in the URL."""
    return f"https://arxiv.org/abs/{base_id}{version or ''}"


def parse_identifier(raw: str) -> dict:
    """Classify an identifier string.

    Returns a dict with ``kind`` in ``{"doi", "arxiv", "unknown"}`` and the
    normalized forms. A ``10.``-prefixed string is a DOI even when it also
    looks like it could be something else; arXiv detection follows.
    """
    s = str(raw).strip()
    result: dict = {"kind": "unknown", "normalized": None, "doi": None,
                    "arxiv_id": None, "version": None}
    candidate = s
    low = candidate.lower()
    is_doi = False
    for pref in _DOI_URL_PREFIXES:
        if low.startswith(pref):
            candidate = candidate[len(pref):].strip()
            is_doi = True
            break
    probe = candidate.strip().lower()
    if probe.startswith("10.") and _DOI_RE.match(probe):
        result.update(kind="doi", normalized=probe, doi=probe)
        return result
    if is_doi:
        return result
    m = _ARXIV_NEW_RE.match(probe)
    if m:
        result.update(kind="arxiv", arxiv_id=m.group(1),
                      version=(f"v{m.group(3)}" if m.group(3) else None),
                      normalized=m.group(1))
        return result
    m = _ARXIV_LEGACY_RE.match(probe)
    if m:
        result.update(kind="arxiv", arxiv_id=m.group(1),
                      version=(f"v{m.group(4)}" if m.group(4) else None),
                      normalized=m.group(1))
        return result
    for pref in ("https://arxiv.org/abs/", "http://arxiv.org/abs/",
                 "https://arxiv.org/pdf/", "http://arxiv.org/pdf/",
                 "arxiv.org/abs/", "arxiv.org/pdf/", "arxiv:"):
        if s.lower().startswith(pref):
            try:
                base, ver = normalize_arxiv_id(s)
                result.update(kind="arxiv", arxiv_id=base, version=ver, normalized=base)
                return result
            except ValueError:
                return result
    return result
