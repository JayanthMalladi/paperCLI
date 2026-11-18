"""Document parsing orchestrator: PDF (child-process isolated), XML, HTML.

Spec section 16 isolates heavy PDF parsing in a child process with a
runtime/content limit; the child receives an argument array (no shell) and
writes one JSON line. XML/HTML parsing is light and runs in-process.

A parse produces a *document parse report* that the ingestion layer stores:

* ``kind`` -- pdf | xml | html
* ``pages`` / ``sections`` -- stable locations
* ``text_index`` -- (page, section, paragraph, text) records for the
  ``document_text`` index (excerpts resolve to exact source text)
* ``identity`` -- embedded title/DOI (wrong-paper guard input)
* ``warnings`` -- extraction warnings (disclosed, never silent)
* ``scanned`` -- True when any page is image-only / empty
* ``page_count``
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from papercli.documents.xml_html import parse_fulltext as parse_html_xml

__all__ = [
    "ParseError",
    "parse_document",
    "parse_pdf_isolated",
    "worker_path",
    "DEFAULT_PARSE_TIMEOUT_S",
    "DEFAULT_MAX_CONTENT_BYTES",
]

#: Heavy-parse runtime ceiling (spec section 16).
DEFAULT_PARSE_TIMEOUT_S = 90.0
#: Content-size ceiling (spec section 16).
DEFAULT_MAX_CONTENT_BYTES = 128 * 1024 * 1024


class ParseError(Exception):
    """A document could not be parsed into stable locations."""


def worker_path() -> Path:
    return Path(__file__).resolve().parent / "_parse_worker.py"


def _python_exe() -> str:
    """The interpreter that runs the worker (the same runtime as the CLI)."""
    return sys.executable


def parse_pdf_isolated(data: bytes, *, timeout: float = DEFAULT_PARSE_TIMEOUT_S,
                       max_bytes: int | None = None,
                       temp_dir: Path | None = None) -> dict[str, Any]:
    """Parse PDF bytes in a child process (spec section 16).

    The bytes are written to a temp file, the worker is spawned with an
    argument array (no shell), and its stdout JSON is read back. A timeout
    or content-size breach raises :class:`ParseError` with a disclosure
    message, never a hang.
    """
    import tempfile
    import os

    mb = max_bytes if max_bytes is not None else DEFAULT_MAX_CONTENT_BYTES
    if len(data) > mb:
        raise ParseError(
            f"content exceeds limit ({len(data)} > {mb} bytes); not parsed")

    # Write the PDF to a temp file the worker reads (keeps the worker's
    # argument array small: a path, not the whole payload).
    base = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
    tf = None
    try:
        tf = tempfile.NamedTemporaryFile(dir=str(base), prefix="papercli-parse-",
                                          suffix=".pdf", delete=False)
        tf.write(data)
        tf.flush()
        tf.close()
        cmd = [_python_exe(), str(worker_path()), tf.name, str(mb)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = ""
            try:
                err = json.loads(stdout or "{}")
                detail = f" ({err.get('error')})" if err.get("error") else ""
            except (ValueError, AttributeError):
                pass
            raise ParseError(f"PDF parse child failed (exit {proc.returncode}){detail}")
        out = json.loads(stdout or "{}")
        if not out.get("ok"):
            raise ParseError(f"PDF parse reported an error: {out.get('error')}")
        return out
    except subprocess.TimeoutExpired as e:
        raise ParseError(
            f"PDF parse timed out after {timeout}s (spec section 16 limit)") from e
    finally:
        if tf is not None:
            try:
                Path(tf.name).unlink(missing_ok=True)
            except OSError:
                pass


def parse_document(data: bytes, kind: str, *,
                   timeout: float = DEFAULT_PARSE_TIMEOUT_S,
                   max_bytes: int | None = None,
                   temp_dir: Path | None = None) -> dict[str, Any]:
    """Parse one document by detected kind; uniform report shape.

    * ``pdf``  -- child-process isolation (spec section 16)
    * ``xml``  / ``html`` -- in-process (light)

    The report carries ``text_index`` records ready for the
    ``document_text`` index and ``identity`` for the wrong-paper guard.
    """
    if kind == "pdf":
        raw = parse_pdf_isolated(data, timeout=timeout, max_bytes=max_bytes,
                                 temp_dir=temp_dir)
        # The child process already built the section structure; use it.
        sections = list(raw.get("sections") or [])
        pages = [
            {"page": p["page"], "text": p.get("text", ""),
             "lines": p.get("lines", []), "has_images": p.get("has_images", False)}
            for p in raw.get("pages", [])
        ]
        warnings = list(raw.get("warnings", []))
        scanned = any(p.get("has_images") and not p.get("text", "").strip()
                      for p in raw.get("pages", []))
        empty = any(not p.get("text", "").strip() for p in raw.get("pages", []))
        if scanned or empty:
            warnings.append("scanned/empty pages detected; OCR is out of scope "
                            "and those pages have no extractable text")
        return {
            "kind": "pdf",
            "pages": pages,
            "sections": sections,
            "text_index": [
                {"page": s["page"], "section": s["section"],
                 "paragraph": s["paragraph"], "text": s["text"]}
                for s in sections
            ],
            "identity": raw.get("metadata", {}),
            "warnings": warnings,
            "scanned": bool(scanned),
            "page_count": int(raw.get("page_count", 0)),
        }

    if kind in ("xml", "html"):
        rep = parse_html_xml(data, kind)
        sections = rep.get("sections", [])
        return {
            "kind": kind,
            "pages": rep.get("pages", []),
            "sections": sections,
            "text_index": [
                {"page": s.get("page", 1), "section": s.get("section", ""),
                 "paragraph": s.get("paragraph", 0), "text": s.get("text", "")}
                for s in sections
            ],
            "identity": rep.get("identity", {}),
            "warnings": list(rep.get("warnings", [])),
            "scanned": False,
            "page_count": int(rep.get("page_count", 0)),
        }

    raise ParseError(f"unsupported document kind: {kind!r}")
