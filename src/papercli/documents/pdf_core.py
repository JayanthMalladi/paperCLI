"""Stdlib PDF text extraction (Phase 3).

Zero external dependencies: objects are located by scanning, top-level
dictionary fields are parsed with a small token scanner, FlateDecode
streams are inflated with ``zlib``, and text-show operators (Tj/TJ/'/\")
are decoded into ordered lines. This is a *text* extractor, not a renderer:
accurate on simple text PDFs; scanned/empty pages are flagged, never
silently accepted (spec section 9; OCR is out of scope and its absence is
disclosed in the parse warnings). Spec section 16: the heavy work runs in
a child process (:mod:`papercli.documents.parser`), which calls these
pure functions.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PdfParseError",
    "PdfPage",
    "PdfParseResult",
    "parse_pdf",
    "scan_pdf_meta",
    "build_pdf_structure",
]

_PDF_MAGIC = b"%PDF"
_L1 = "latin-1"

#: One-pass tokenizer for content streams and object bodies.
_TOKEN = re.compile(
    r"""
    (?P<string>\((?:[^()\\]|\\.)*\))
  | (?P<arr>\[[^\[\]]*\])
  | (?P<dict>\<\<.*?\>\>)
  | (?P<ref>\d+\s+\d+\s+R)
  | (?P<name>/[^\s\[\]<>()%/\*]+)
  | (?P<num>[+\-]?\d*\.?\d+)
  | (?P<op>[^()\[\]<>/\s][^()\[\]<>/\s]*)
    """,
    re.VERBOSE | re.DOTALL,
)

_OBJ_HEAD = re.compile(r"(?<!\d)(\d+)\s+(?:\d+)\s+obj\b")
_DOI_CAND = re.compile(r"10\.\d{4,9}/[A-Za-z0-9./+_-]{2,120}")
_STREAM_HEAD = re.compile(r"stream\r?\n")


class PdfParseError(ValueError):
    """The bytes are not a parseable PDF (or are corrupt)."""


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _decode_literal_string(token: str) -> str:
    """Decode a PDF literal string token ``( ... )`` with escapes."""
    body = token[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        i += 1
        if ch != "\\":
            out.append(ch)
            continue
        if i >= len(body):
            break
        esc = body[i]
        i += 1
        if esc == "n":
            out.append("\n")
        elif esc == "r":
            out.append("\r")
        elif esc == "t":
            out.append("\t")
        elif esc == "b":
            out.append("\b")
        elif esc == "f":
            out.append("\f")
        elif esc in ("(", ")"):
            out.append(esc)
        elif esc.isdigit():
            octal = esc
            while i < len(body) and body[i].isdigit() and len(octal) < 3:
                octal += body[i]
                i += 1
            out.append(chr(int(octal, 8) & 0xFF))
        else:
            out.append(esc)
    return "".join(out)


def _decode_hex_string(token: str) -> str:
    hexs = token[1:-1].replace(" ", "").replace("\n", "").replace("\r", "")
    if len(hexs) % 2:
        hexs += "0"
    try:
        return bytes.fromhex(hexs).decode(_L1)
    except ValueError:
        return ""


def _ref_num(value_text: str) -> int | None:
    m = re.match(r"\s*(\d+)\s+(\d+)\s+R\b", value_text)
    return int(m.group(1)) if m else None


def _parse_top_dict(body_text: str) -> dict[str, str]:
    """Parse one top-level PDF dictionary into ``/Name -> raw value token``.

    Handles nested ``<< >>`` dicts and ``[ ]`` arrays as opaque raw spans,
    and strings with escaped parens. The object body is the text between
    ``obj`` and ``endobj``; the outermost dictionary is stripped first.
    """
    text = body_text.strip()
    # Strip the outermost "<< ... >>" when present.
    if text.startswith("<<"):
        end = _dict_span(text, 0)
        text = end[2:-2].strip()  # inside the outermost braces
    fields: dict[str, str] = {}
    i = 0
    n = len(text)
    while i < n:
        # find next token
        m = _TOKEN.search(text, i)
        if m is None:
            break
        kind = m.lastgroup
        tok = m.group(0)
        i = m.end()
        if kind != "name":
            continue
        # After a name, capture its value token.
        vm = re.compile(r"\s*").match(text, i)
        j = vm.end() if vm else i
        if j >= n:
            fields[tok] = ""
            break
        ch = text[j]
        if ch == "(":
            mm = re.compile(r"\((?:[^()\\]|\\.)*\)").match(text, j)
            val = mm.group(0) if mm else ""
            i = j + len(val)
        elif ch == "[":
            mm = re.compile(r"\[[^\[\]]*\]").match(text, j)
            val = mm.group(0) if mm else ""
            i = j + len(val)
        elif text[j:j + 2] == "<<":
            val = _dict_span(text, j)
            i = j + len(val)
        elif ch == "<":
            mm = re.compile(r"<[^<>]*>").match(text, j)
            val = mm.group(0) if mm else ""
            i = j + len(val)
        else:
            # name / ref / number / keyword
            mm = re.compile(r"\d+\s+\d+\s+R|/[^\s\[\]<>()%/\\*]+|[+\-]?\d*\.?\d*|[A-Za-z]+").match(text, j)
            val = mm.group(0) if mm else ""
            i = j + len(val)
        fields[tok] = val.strip()
    return fields


def _dict_span(text: str, pos: int) -> str:
    """Balanced ``<< ... >>`` span starting at ``text[pos]``."""
    depth = 0
    i = pos
    n = len(text)
    while i < n:
        if text[i:i + 2] == "<<":
            depth += 1
            i += 2
            continue
        if text[i:i + 2] == ">>":
            depth -= 1
            i += 2
            if depth == 0:
                return text[pos:i]
            continue
        i += 1
    return text[pos:]


def _obj_fields(body_text: str) -> dict[str, str]:
    """Top-level ``/Name -> raw value token`` for a PDF object body."""
    return _parse_top_dict(body_text)

def _array_items(raw: str) -> list[str]:
    inner = raw[1:-1]
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "(":
            m = re.match(r"\((?:[^()\\]|\\.)*\)", inner[i:])
            if m:
                cur.append(m.group(0))
                i += len(m.group(0))
                continue
        elif ch in "[]" and ch == "[":
            m = re.match(r"\[[^\[\]]*\]", inner[i:])
            if m:
                cur.append(m.group(0))
                i += len(m.group(0))
                continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            if not (ch == "," and depth == 0):
                cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return [it for it in items if it]


def _array_refs(raw: str) -> list[int]:
    """All object numbers in a raw PDF array of references.

    PDF arrays are whitespace-separated (not comma-separated), so this
    scans for ``N G R`` triples wherever they appear inside the array.
    """
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    out: list[int] = []
    for m in re.finditer(r"(\d+)\s+(\d+)\s+R\b", inner):
        out.append(int(m.group(1)))
    return out


def _iter_objects(data: bytes):
    """Sequential scan of top-level PDF objects.

    Returns ``(obj_map, streams, raw)``:

    * ``obj_map`` -- {obj num: fields dict}; each dict is annotated with
      ``$num`` and, for page-tree parents, a resolved integer ``/Kids`` list
      (the values that :func:`_resolve_node` walks).
    * ``streams`` -- {obj num: decoded stream bytes} for FlateDecode content.
    * ``raw`` -- the (num, fields, stream) tuples, for diagnostics.

    Scanning continues *from the end of each object body* so stream payloads
    are never mistaken for object headers.
    """
    text = data.decode(_L1)
    objs: list[tuple[int, dict[str, str], bytes | None]] = []
    pos = 0
    while True:
        m = _OBJ_HEAD.search(text, pos)
        if m is None:
            break
        num = int(m.group(1))
        end = text.find("endobj", m.end())
        body_text = text[m.end():end if end != -1 else len(text)]
        pos = end + 6 if end != -1 else len(text)
        fields = _obj_fields(body_text)
        stream: bytes | None = None
        body_bytes = data[m.end():end if end != -1 else len(data)]
        sm = re.search(rb"(?<!\w)stream\r?\n", body_bytes)
        if sm is not None:
            data_start = sm.end()
            rs = body_bytes.rfind(b"endstream")
            raw = body_bytes[data_start:rs if rs != -1 else len(body_bytes)]
            if raw.endswith(b"\r\n") or raw.endswith(b"\n"):
                raw = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1]
            stream = _decode_stream(fields, raw)
        objs.append((num, fields, stream))

    # Build the resolution maps: fields get "$num"; /Kids arrays of refs get
    # resolved element-wise by _resolve_node (which handles int refs).
    obj_map: dict[int, dict[str, Any]] = {}
    streams: dict[int, bytes] = {}
    for num, fields, stream in objs:
        node = dict(fields)
        node["$num"] = num
        kids_raw = fields.get("/Kids")
        if isinstance(kids_raw, str) and kids_raw.startswith("["):
            node["/Kids"] = _array_refs(kids_raw)
        obj_map[num] = node
        if stream is not None:
            streams[num] = stream
    return obj_map, streams, [(n, f, s) for n, f, s in objs]


def _decode_stream(fields: dict[str, str], raw: bytes) -> bytes:
    """Apply /Filter to a raw stream payload (FlateDecode is the common case)."""
    filt = fields.get("/Filter", "")
    filters = ([filt] if not filt.startswith("[")
               else [f for f in _array_items(filt) if f.startswith("/")])
    out = raw
    for name in filters:
        leaf = name[1:] if name.startswith("/") else name
        if leaf == "FlateDecode":
            inflated = None
            for wbits in (15, -15, 31):
                try:
                    inflated = zlib.decompress(raw, wbits)
                    break
                except zlib.error:
                    continue
            if inflated is None:
                return raw  # undecodable stream: leave raw, caller flags it
            out = inflated
        elif leaf == "ASCIIHexDecode":
            hexs = re.sub(rb"[^0-9a-fA-F]", b"", out)
            if len(hexs) % 2:
                hexs += b"0"
            out = bytes.fromhex(hexs.decode("ascii"))
        elif leaf == "ASCII85Decode":
            import base64
            out = base64.a85decode(out.rstrip(b"~>"), adobe=False)
        # Other filters (DCTDecode=JPEG, etc.): image data -- irrelevant to
        # text extraction; the page will be flagged as scanned/image.
    return out


def _resolve_node(value: Any, obj_map: dict[int, dict[str, Any]],
                  streams: dict[int, bytes], depth: int = 0) -> Any:
    """Resolve a field value to an annotated node dict.

    Handles raw reference tokens (``"2 0 R"``), nested dict spans, and
    integer refs. Annotated nodes carry ``$num`` (their object number),
    ``$kids`` (resolved page-tree children, in order), and ``$content``
    (the page's decoded content-stream bytes).
    """
    if depth > 5 or value is None:
        return value
    if isinstance(value, str):
        ref_num = _ref_num(value)
        if ref_num is not None:
            return _resolve_node(ref_num, obj_map, streams, depth + 1)
        if value.startswith("<<"):
            fields = _obj_fields(value)
            return _annotate(fields, 0, obj_map, streams)
        return value
    if isinstance(value, int):
        fields = obj_map.get(value)
        if fields is None:
            return None
        node = _annotate(dict(fields), value, obj_map, streams)
        # /Kids in obj_map is an int list (pre-resolved by _iter_objects)
        # or a raw ref array: resolve to annotated child nodes.
        kids = node.get("/Kids")
        if isinstance(kids, (list, str)):
            nums = kids if isinstance(kids, list) else _array_refs(kids)
            node["$kids"] = [
                _resolve_node(n, obj_map, streams, depth + 1)
                for n in nums
                if isinstance(n, int) and obj_map.get(n) is not None
            ]
            node.pop("/Kids", None)
        return node
    if isinstance(value, dict):
        return value
    return value


def _annotate(fields: dict[str, Any], num: int,
              obj_map: dict[int, dict[str, Any]],
              streams: dict[int, bytes]) -> dict[str, Any]:
    """Attach ``$num``, resolve ``/Kids`` refs, and carry a page's stream."""
    node: dict[str, Any] = dict(fields) if fields else {}
    node["$num"] = num
    kids_raw = node.get("/Kids")
    if isinstance(kids_raw, list):
        nums: list[int] = kids_raw
    elif isinstance(kids_raw, str):
        nums = _array_refs(kids_raw)
    else:
        nums = []
    if nums:
        node["$kids"] = [
            _annotate(dict(obj_map.get(n, {})), n, obj_map, streams)
            for n in nums
            if isinstance(n, int) and obj_map.get(n) is not None
        ]
    node.pop("/Kids", None)
    contents_raw = node.get("/Contents")
    if isinstance(contents_raw, int):
        cs = streams.get(contents_raw)
        if cs:
            node["$content"] = cs
    elif isinstance(contents_raw, str):
        crn = _ref_num(contents_raw)
        if crn is not None:
            if crn in streams:
                node["$content"] = streams[crn]
        elif contents_raw.startswith("["):
            nums = _array_refs(contents_raw)
            chunks = [streams[n] for n in nums if n in streams]
            if chunks:
                node["$content"] = b"\n".join(chunks)
    return node


def _page_order(catalog_fields: dict[str, str],
                 obj_map: dict[int, dict[str, Any]],
                 streams: dict[int, bytes]) -> list[dict[str, Any]]:
    """Walk the /Pages tree in document order; return page nodes."""
    result: list[dict[str, Any]] = []
    pages_root = _resolve_node(catalog_fields.get("/Pages"), obj_map, streams)

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 40 or not isinstance(node, dict):
            return
        kids = node.get("$kids")
        if isinstance(kids, list):
            for child in kids:
                if isinstance(child, dict):
                    walk(child, depth + 1)
            return
        if node.get("/Type") in ("/Page", "Page"):
            resources = _resolve_node(node.get("/Resources"), obj_map, streams)
            content = node.get("$content", b"")
            result.append({
                "node": node,
                "$content": content if isinstance(content, bytes) else b"",
            })

    if isinstance(pages_root, dict):
        walk(pages_root)
    return result


def _node_fields(d: dict[str, Any]) -> dict[str, str]:
    """Strip the ``$``-annotated keys (internal) from a resolved node."""
    return {k: v for k, v in d.items() if not k.startswith("$")}





# ---------------------------------------------------------------------------
# Content-stream text extraction
# ---------------------------------------------------------------------------

#: Operator/keyword token: letters and ``*`` (the text-marking operators),
#: plus the ``'`` and ``"`` operators that appear as bare tokens.
_OP_TOKEN = re.compile(r"[A-Za-z*]+|\x27|\x22")


def _tokens(content: bytes):
    """Byte-level tokenizer for a content stream.

    Yields ``(token, kind)`` where kind is one of
    ``string | array | dict | ref | num | name | op``.
    """
    text = content.decode(_L1)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "%":
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "(":
            m = re.compile(r"\((?:[^()\\]|\\.)*\)").match(text, i)
            if m:
                yield m.group(0), "string"
                i = m.end()
                continue
            i += 1
            continue
        if ch == "[":
            m = re.compile(r"\[[^\[\]]*\]").match(text, i)
            if m:
                yield m.group(0), "array"
                i = m.end()
                continue
            i += 1
            continue
        if ch == "<" and text[i:i + 2] == "<<":
            span = _dict_span(text, i)
            yield span, "dict"
            i += len(span)
            continue
        if ch == "<":
            m = re.compile(r"<[^<>]*>").match(text, i)
            if m:
                yield m.group(0), "string"
                i = m.end()
                continue
            i += 1
            continue
        if ch == "/":
            m = re.compile(r"/[^\s\[\]<>()%/\\*]+").match(text, i)
            if m:
                yield m.group(0), "name"
                i = m.end()
                continue
            i += 1
            continue
        if ch.isdigit() or ch in "+-.":
            m = re.compile(r"[+\-]?\d*\.?\d*").match(text, i)
            if m:
                tok = m.group(0)
                rest = text[m.end():]
                mr = re.match(r"^\s+(\d+)\s+R\b", rest)
                if re.fullmatch(r"[+-]?\d+", tok) and mr:
                    yield (int(tok), int(mr.group(1))), "ref"
                    i = m.end() + mr.end()
                    continue
                yield (float(tok) if "." in tok else int(tok)), "num"
                i = m.end()
                continue
            i += 1
            continue
        m = _OP_TOKEN.match(text, i)
        if m:
            yield m.group(0), "op"
            i = m.end()
            continue
        i += 1


def _show_text(tok: str) -> str:
    """Decode one ``( ... )`` or ``<...>`` string token into characters."""
    if tok.startswith("("):
        return _decode_literal_string(tok)
    if tok.startswith("<"):
        return _decode_hex_string(tok)
    return tok


def _decode_page_content(content: bytes, resources_node: Any, obj_map: dict, streams: dict[int, bytes]) -> tuple[str, list[tuple[float, str]], bool, list[str]]:
    """Decode one page's content stream into ordered text lines.

    Returns ``(text, has_images, warnings)``. Position state follows the
    PDF text operators (BT/ET, Tf, Td/TD/Tm/TL/T*, Tj, TJ, ', "); font
    encodings are not resolved (CID/ToUnicode is out of scope) so
    glyph codes that do not map to printable text yield no output for
    that run -- the page is flagged instead of silently accepted.
    """
    warnings: list[str] = []
    has_images = False
    if isinstance(resources_node, dict):
        xo = _node_fields(resources_node).get("/XObject")
        # /XObject may be a resolved dict node or an opaque raw span.
        if isinstance(xo, str) and xo.startswith("<<"):
            xo = _obj_fields(xo)
        if isinstance(xo, dict):
            for v in xo.values():
                sub = _resolve_node(v, obj_map, streams) if isinstance(v, str) else v
                if isinstance(sub, dict):
                    st = _node_fields(sub).get("/Subtype")
                    if st in ("/Image", "Image"):
                        has_images = True

    stack: list[Any] = []
    tx = ty = 0.0
    leading = 0.0
    font_size = 0.0
    items: list[tuple[float, float, float, str]] = []

    for tok, kind in _tokens(content):
        if kind in ("string", "array", "dict", "name", "num", "ref"):
            stack.append(tok)
            continue
        op = tok

        def _pop() -> Any:
            return stack.pop() if stack else None

        if op == "BT":
            tx = ty = 0.0
        elif op == "Tf":
            size = _pop()
            _pop()
            if isinstance(size, (int, float)):
                font_size = abs(float(size))
        elif op in ("Td", "TD"):
            dy = _pop()
            dx = _pop()
            if op == "TD" and isinstance(dy, (int, float)):
                leading = -float(dy)
            if isinstance(dx, (int, float)) and isinstance(dy, (int, float)):
                tx += float(dx)
                ty += float(dy)
        elif op == "Tm":
            vals = [stack.pop() for _ in range(6)][::-1]
            if all(isinstance(v, (int, float)) for v in vals):
                tx, ty = float(vals[0]), float(vals[1])
        elif op == "TL":
            v = _pop()
            if isinstance(v, (int, float)):
                leading = float(v)
        elif op == "T*":
            ty -= leading
        elif op == "Tj":
            s = _pop()
            if isinstance(s, str):
                t = _show_text(s)
                if t:
                    items.append((ty, tx, font_size, t))
        elif op == "TJ":
            arr = _pop()
            if isinstance(arr, str) and arr.startswith("["):
                parts: list[str] = []
                for part in _array_items(arr):
                    if part.startswith("("):
                        parts.append(_show_text(part))
                    elif part.startswith("<"):
                        parts.append(_show_text(part))
                    else:
                        try:
                            gap = float(part)
                        except ValueError:
                            continue
                        if gap <= -100:
                            parts.append(" ")
                joined = "".join(parts)
                if joined:
                    items.append((ty, tx, font_size, joined))
        elif op == "'":
            s = _pop()
            ty -= leading
            if isinstance(s, str):
                t = _show_text(s)
                if t:
                    items.append((ty, tx, font_size, t))
        elif op == '"':
            _pop()  # char space
            _pop()  # word space
            s = _pop()
            ty -= leading
            if isinstance(s, str):
                t = _show_text(s)
                if t:
                    items.append((ty, tx, font_size, t))
        else:
            # Unknown operators: their operands are ambiguous; reset.
            stack = []

    items.sort(key=lambda it: (-it[0], it[1]))
    ylines: list[tuple[float, str]] = []
    cur_y: float | None = None
    for yv, _xv, _sz, txt in items:
        if not txt.strip():
            continue
        if cur_y is None or abs(yv - cur_y) > 2.5:
            ylines.append((yv, txt))
            cur_y = yv
        else:
            ylines[-1] = (ylines[-1][0], (ylines[-1][1] + " " + txt).strip())
    lines = [txt for _y, txt in ylines]
    text = "\n".join(lines)
    return text, ylines, has_images, warnings


# ---------------------------------------------------------------------------
# Metadata + wrong-paper guard inputs (spec section 9 / acceptance 'Identity')
# ---------------------------------------------------------------------------

def scan_pdf_meta(data: bytes) -> dict[str, Any]:
    """Embedded title/DOI candidates for the wrong-paper identity check.

    Scans the raw bytes: /Title string values and DOI-shaped patterns
    (e.g. ``10.1109/...``), tolerating escaped backslashes the way PDF
    literal strings carry them.
    """
    text = data.decode(_L1)
    title: str | None = None
    for m in re.finditer(r"/Title\s*\((?:[^()\\]|\\.)*\)", text):
        raw = m.group(0)
        lp = raw.index("(")
        cand = _decode_literal_string(raw[lp:])
        cand = cand.strip()
        if cand:
            title = cand
            break
    dois: list[str] = []
    # DOI patterns in the raw bytes; unescape \dd sequences PDF strings use.
    for m in _DOI_CAND.finditer(data[: 1024 * 1024].decode(_L1)):
        cand = re.sub(r"\\\d+", "", m.group(0))
        cand = cand.strip(".,;)")
        cand = cand.lower()
        if re.fullmatch(r"10\.\d{4,9}/[A-Za-z0-9./+_-]+", cand):
            dois.append(cand)
        if len(dois) >= 8:
            break
    return {
        "title": title,
        "doi": dois[0] if dois else None,
        "dois_found": sorted(set(dois)),
    }


@dataclass
class PdfPage:
    page: int
    text: str
    lines: list[str] = field(default_factory=list)
    has_images: bool = False
    #: Y-coordinate of each line (PDF space), aligned with ``lines``.
    line_y: list[float] = field(default_factory=list)


@dataclass
class PdfParseResult:
    pages: list[PdfPage]
    metadata: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def parse_pdf(data: bytes) -> PdfParseResult:
    """Extract numbered text pages + embedded metadata from raw PDF bytes."""
    if not data.startswith(_PDF_MAGIC):
        raise PdfParseError("not a PDF (missing %PDF magic)")
    obj_map, streams, _objs = _iter_objects(data)
    if not obj_map:
        raise PdfParseError("no PDF objects found; file may be corrupt")

    catalog: dict | None = None
    for d in obj_map.values():
        if _node_fields(d).get("/Type") in ("/Catalog", "Catalog"):
            catalog = d
            break
    if catalog is None:
        for num in sorted(obj_map):
            if "/Pages" in obj_map[num] or "/Kids" in obj_map[num]:
                catalog = obj_map[num]
                break
    if catalog is None:
        result = PdfParseResult(pages=[], metadata=scan_pdf_meta(data),
                                warnings=["no catalog found; PDF may be corrupt"])
        return result

    page_nodes = _page_order(catalog, obj_map, streams)
    result = PdfParseResult(pages=[], metadata=scan_pdf_meta(data),
                             warnings=[])
    if not page_nodes:
        result.warnings.append("no pages found in PDF (image-only or corrupt)")

    for idx, node in enumerate(page_nodes, start=1):
        node2 = node["node"]
        resources = _resolve_node(node2.get("/Resources"), obj_map, streams)
        content = node.get("$content", b"")
        text, ylines, has_images, warns = _decode_page_content(content, resources, obj_map, streams)
        for w in warns:
            result.warnings.append(f"page {idx}: {w}")
        if not text.strip():
            if has_images:
                result.warnings.append(
                    f"page {idx}: scanned page (embedded image); OCR is out "
                    "of scope and no text was extracted")
            else:
                result.warnings.append(f"page {idx}: empty (no extractable text)")
        lines = [txt for _y, txt in ylines]
        result.pages.append(PdfPage(page=idx, text=text, lines=lines,
                                    has_images=has_images,
                                    line_y=[y for y, _ in ylines]))
    return result


# ---------------------------------------------------------------------------
# Section / paragraph structuring (stable locations for evidence, spec 9)
# ---------------------------------------------------------------------------

_KNOWN_SECTIONS = {
    "abstract", "introduction", "background", "methods", "method",
    "materials and methods", "results", "discussion", "conclusion",
    "conclusions", "limitations", "references", "acknowledgements",
    "acknowledgments", "appendix",
}

_HEADING_NUM = re.compile(r"^(\d+(?:\.\d+)*)\s+[A-Z][A-Za-z0-9&\-]{2,60}$")
_HEADING_CAPS = re.compile(r"^[A-Z][A-Za-z&\-/]{2,50}$")


def _is_heading(line: str) -> str | None:
    """Return the section name when ``line`` looks like a section heading."""
    s = " ".join(line.split())
    if len(s) > 60:
        return None
    if s.lower() in _KNOWN_SECTIONS:
        return s
    if _HEADING_NUM.match(s):
        return s
    if " " in s and _HEADING_CAPS.match(s):
        return s
    return None


def build_pdf_structure(pages: list[PdfPage]) -> list[dict[str, Any]]:
    """Group extracted page lines into stable section/paragraph locations.

    Paragraphs break at heading lines or on a vertical gap larger than
    1.4x the typical line height of the page. Returns a flat list of
    ``{"page", "section", "paragraph", "text"}`` dicts in document order,
    ready for the ``document_text`` index.
    """
    out: list[dict[str, Any]] = []
    for pg in pages:
        if pg.line_y:
            ylines = list(zip(pg.line_y, pg.lines))
        else:
            ylines = [(0.0, ln) for ln in pg.lines]
        if not ylines:
            continue
        gaps = sorted(
            y2 - y1 for (y1, _), (y2, _) in zip(ylines, ylines[1:]) if y2 - y1 > 0
        )
        typical = gaps[len(gaps) // 2] if gaps else 12.0
        threshold = max(1.4 * typical, 4.0)

        section = ""
        para_no = 0
        para_lines: list[str] = []
        prev_y: float | None = None

        def flush() -> None:
            nonlocal para_lines, para_no
            text = " ".join(" ".join(l.split()) for l in para_lines).strip()
            para_lines = []
            if not text:
                return
            out.append({
                "page": pg.page,
                "section": section,
                "paragraph": para_no,
                "text": text,
            })
            para_no += 1

        for y, line in ylines:
            if prev_y is not None and (y - prev_y) > threshold:
                flush()
            heading = _is_heading(line)
            if heading is not None:
                flush()
                section = heading
                para_no = 0
            else:
                para_lines.append(line)
            prev_y = y
        flush()
    return out
