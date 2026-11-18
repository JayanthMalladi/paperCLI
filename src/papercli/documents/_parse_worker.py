"""PDF parse worker (spec section 16: heavy PDF parsing in a child process).

Invoked with an argument array -- never a shell -- by
:mod:`papercli.documents.parser`. The worker reads one PDF path, runs the
stdlib extractor, and writes a single JSON document to stdout. Keeping the
worker self-contained (no package import) means a hung or crashed parse
cannot take the CLI process down with it.

Usage (argument array, run by the parent)::

    <python> _parse_worker.py <pdf-path> [max-bytes]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv:
        print(json.dumps({"ok": False, "error": "usage: _parse_worker <pdf-path> [max-bytes]"}))
        return 2
    path = Path(argv[0])
    max_bytes = int(argv[1]) if len(argv) > 1 else 0
    try:
        # Best-effort path to the installed/src package for pdf_core.
        here = Path(__file__).resolve()
        for candidate in (
            here.parent.parent.parent,      # <...>/src when running from src
            here.parent.parent.parent.parent,
        ):
            if (candidate / "papercli").is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
                break
        # If nothing found, the package must already be importable.
        from papercli.documents.pdf_core import parse_pdf, build_pdf_structure

        data = path.read_bytes()
        if max_bytes > 0 and len(data) > max_bytes:
            print(json.dumps({"ok": False,
                              "error": f"file exceeds content limit ({len(data)} > {max_bytes} bytes)"}))
            return 2
        result = parse_pdf(data)
        pages = [
            {"page": p.page, "text": p.text, "lines": p.lines,
             "line_y": p.line_y, "has_images": p.has_images}
            for p in result.pages
        ]
        from papercli.documents.pdf_core import PdfPage
        page_objs = [PdfPage(page=p["page"], text=p.get("text", ""),
                              lines=p.get("lines", []),
                              line_y=p.get("line_y", []),
                              has_images=p.get("has_images", False))
                     for p in pages]
        sections = build_pdf_structure(page_objs)
        out = {
            "ok": True,
            "sections": sections,
            "pages": pages,
            "page_count": result.page_count,
            "warnings": result.warnings,
            "metadata": result.metadata,
        }
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as e:  # noqa: BLE001 -- report, never crash the parent
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}",
                          "exception_type": type(e).__name__},
                         ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
