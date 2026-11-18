"""HTML/XML full-text parsing (Phase 3).

Repository XML (JATS, arXiv Atom, plain XML) is preferred over HTML:
structured paragraph elements map directly to stable paragraph locations,
which the evidence layer (spec section 9) requires. HTML is the fallback
when no XML is available.

Both parsers are stdlib-only (``xml.etree`` + a small HTML block extractor)
and produce the same shape as the PDF pipeline:

* ``pages`` -- logical (XML/HTML are not paginated; page is 1);
* ``sections`` -- flat ``{"section", "paragraph", "text"}`` records with
  ``page`` = 1;
* ``identity`` -- embedded title + DOI candidates for the wrong-paper guard;
* ``warnings`` -- parse warnings (disclosed, never silent).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

__all__ = [
    "parse_fulltext",
    "parse_xml_fulltext",
    "parse_html_fulltext",
]

# pub-type="doi" attribute (["\x27] matches a double or single quote).
_DOI_ATTR_RX = re.compile(r'<article-id[^>]*pub-type=[\x22\x27]doi[^>]*>([^<]+)<')
_DOI_ANY_RX = re.compile(r'<article-id[^>]*>([^<]+)</article-id>')
_META_DOI_RX = re.compile(
    r'<meta[^>]+name=[\x22\x27]citation_doi[\x22\x27][^>]*content=[\x22\x27]([^\x22\x27]+)',
    re.I,
)
_TITLE_RX = re.compile(r'<title[^>]*>(.*?)</title\s*>', re.I | re.S)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _text_of(elem: ET.Element) -> str:
    parts: list[str] = [elem.text or ""]
    for child in elem:
        parts.append(_text_of(child))
        parts.append(child.tail or "")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _xml_identity(root: ET.Element) -> dict[str, Any]:
    title: str | None = None
    # Prefer the article-meta/front context (the paper's own title), so a
    # section <title> ("Methods", ...) is never mistaken for it.
    for meta_tag in ("article-meta", "front", "article"):
        meta = next((n for n in root.iter(meta_tag)), None)
        if meta is None:
            continue
        for t in list(meta.iter("article-title"))[:2]:
            txt = _text_of(t)
            if txt:
                title = txt
                break
        if title:
            break
        for t in list(meta.iter("title"))[:2]:
            txt = _text_of(t)
            if txt and len(txt) >= 4:
                title = txt
                break
        if title:
            break
    if title is None:
        cands = [_text_of(t) for t in root.iter("title")]
        cands = [c for c in cands if c and len(c) >= 4]
        if cands:
            title = max(cands, key=len)
    doi: str | None = None
    xml_text = ET.tostring(root, encoding="unicode")
    m = _DOI_ATTR_RX.search(xml_text)
    if m:
        doi = m.group(1).strip()
    else:
        m = _DOI_ANY_RX.search(xml_text)
        if m:
            doi = m.group(1).strip()
    return {"title": title, "doi": doi}


def _xml_sections(root: ET.Element) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract (section, paragraph, text) records from a full-text XML root."""
    warnings: list[str] = []
    out: list[dict[str, Any]] = []

    body: ET.Element | None = None
    for tag in ("body", "article-body", "full-text", "content", "article"):
        nodes = list(root.iter(tag))
        if nodes:
            body = nodes[0]
            break
    if body is None:
        body = root

    def handle_section(sec: ET.Element, inherited: str, para_no_ref: list[int]) -> None:
        title_el = sec.find("title")
        sec_name = _text_of(title_el) if title_el is not None else inherited
        if sec_name != inherited:
            para_no_ref[0] = 0
        for child in sec:
            tag = _local(child.tag)
            if tag == "title":
                continue
            if tag in ("sec", "section"):
                handle_section(child, sec_name, para_no_ref)
            elif tag in ("p", "abstract", "fig", "table", "list", "item",
                         "para", "para-group"):
                text = _text_of(child)
                if text:
                    out.append({"page": 1, "section": sec_name or inherited,
                                "paragraph": para_no_ref[0], "text": text})
                    para_no_ref[0] += 1

    pno = [0]
    for ab in list(body.iter("abstract")):
        text = _text_of(ab)
        if text:
            out.append({"page": 1, "section": "Abstract", "paragraph": 0, "text": text})
    secs = list(body.iter("sec"))
    if not secs:
        secs = list(body.iter("section"))
    if secs:
        for s in secs:
            handle_section(s, "", pno)
    else:
        # No <sec>/<section>: paragraphs directly under body (Atom/unknown).
        for child in list(body):
            tag = _local(child.tag)
            if tag in ("p", "para", "paragraph", "summary", "abstract",
                      "section", "title"):
                text = _text_of(child)
                if text:
                    out.append({"page": 1, "section": "", "paragraph": pno[0],
                                "text": text})
                    pno[0] += 1
        if not out:
            warnings.append("XML parsed but no paragraphs found "
                            "(unsupported full-text structure)")
    return out, warnings


def parse_xml_fulltext(data: bytes) -> dict[str, Any]:
    """Parse repository/Atom/plain XML full text into stable locations."""
    root: ET.Element | None = None
    last_err: Exception | None = None
    for candidate in (data, data.lstrip(b"\xef\xbb\xbf"),
                      re.sub(rb"^[\s\x00-\x08\x0b\x0c]+", b"", data)):
        try:
            root = ET.fromstring(candidate)
            break
        except ET.ParseError as e:
            last_err = e
            continue
    if root is None:
        return {"kind": "xml", "pages": [], "sections": [], "identity": {},
                "warnings": [f"XML unparseable: {last_err}"], "page_count": 0}
    sections, warnings = _xml_sections(root)
    return {
        "kind": "xml",
        "pages": [{"page": 1,
                   "text": " ".join(s["text"] for s in sections),
                   "lines": [s["text"] for s in sections]}],
        "sections": sections,
        "identity": _xml_identity(root),
        "warnings": warnings,
        "page_count": 1 if sections else 0,
    }


_SCRIPT_RX = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RX = re.compile(r"</?[a-zA-Z][^>]*>", re.S)
_BLOCK_OPEN = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")


def parse_html_fulltext(data: bytes) -> dict[str, Any]:
    """Parse an HTML page (landing or HTML full text) into paragraphs.

    Paragraphs come from block elements; ``<h1>-<h4>`` open a new section
    label. Best-effort structural parse, not a renderer: noisy chrome stays
    out of the paragraphs and is disclosed via warnings.
    """
    text = data.decode("utf-8", "replace")
    text = _SCRIPT_RX.sub("", text)
    warnings: list[str] = []

    t_m = _TITLE_RX.search(text)
    d_m = _META_DOI_RX.search(text)
    identity = {"title": t_m.group(1).strip() if t_m else None,
                "doi": d_m.group(1).strip() if d_m else None}

    sections: list[dict[str, Any]] = []
    current_section = ""
    para_no = 0

    # Tokenize: tags in order, with the plain text between consecutive tags.
    pos = 0
    pending_heading: str | None = None
    pending_heading_buf = ""
    tag_rx = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", re.S)
    while True:
        m = tag_rx.search(text, pos)
        if m is None:
            tail = text[pos:]
            if pending_heading is not None:
                pending_heading_buf += tail
            pos = None
            break
        # Text between the previous tag end and this tag start.
        between = text[pos:m.start()]
        if pending_heading is not None:
            pending_heading_buf += between
        pos = m.end()
        closing = m.group(1) == "/"
        tag_name = m.group(2).lower()
        if pending_heading is not None:
            if closing and tag_name == pending_heading:
                label = re.sub(r"\s+", " ", pending_heading_buf).strip()
                sections.append({"page": 1, "section": current_section,
                                 "paragraph": para_no, "text": label})
                para_no += 1
                current_section = label or current_section
                pending_heading = None
                pending_heading_buf = ""
            continue
        if not closing and tag_name in ("h1", "h2", "h3", "h4"):
            pending_heading = tag_name
            pending_heading_buf = ""
            continue
        if not closing and tag_name in ("p", "li", "td", "th", "div", "dd", "dt"):
            close_m = re.search(rf"</{tag_name}\s*>", text[pos:], re.I)
            if not close_m:
                continue
            block = text[pos:pos + close_m.start()]
            clean = re.sub(r"<[^>]+>", " ", block)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                sections.append({"page": 1, "section": current_section,
                                 "paragraph": para_no, "text": clean})
                para_no += 1
            pos = pos + close_m.end()
            continue
        # Any other tag: skip past it (its inner text belongs to the block
        # that started it, if one was open).

    if pending_heading is not None:
        # Unclosed heading: use whatever accumulated.
        label = re.sub(r"\s+", " ", pending_heading_buf).strip()
        if label:
            sections.append({"page": 1, "section": current_section,
                             "paragraph": para_no, "text": label})
            para_no += 1

    if not sections:
        flat = re.sub(r"<[^>]+>", " ", text)
        flat = re.sub(r"\s+", " ", flat).strip()
        if flat:
            sections.append({"page": 1, "section": "", "paragraph": 0,
                             "text": flat[:4000]})
            warnings.append("no block elements found; used page-text fallback "
                            "(truncated to 4000 chars)")
    return {
        "kind": "html",
        "pages": [{"page": 1,
                   "text": " ".join(s["text"] for s in sections),
                   "lines": [s["text"] for s in sections]}],
        "sections": sections,
        "identity": identity,
        "warnings": warnings,
        "page_count": 1 if sections else 0,
    }


def parse_fulltext(data: bytes, kind: str) -> dict[str, Any]:
    """Dispatch XML/HTML full text by detected document kind."""
    if kind == "xml":
        return parse_xml_fulltext(data)
    if kind == "html":
        return parse_html_fulltext(data)
    raise ValueError(f"unsupported full-text kind: {kind!r}")
