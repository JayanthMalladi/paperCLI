"""Retrieval cache for provider responses and full-text bytes (Phase 2).

The cache is keyed by request URL (spec section 8: "cache metadata with a
configurable TTL; use shorter TTL for negative availability"). It has:

* a **metadata store** -- provider responses (Unpaywall DOI records,
  Crossref/OpenAlex/arXiv/S2 bodies) with a configurable per-kind TTL;
* a **negative store** -- unavailable / rate-limited results kept with a
  *shorter* TTL so transient 429s are re-tried sooner;
* a **document store** -- retrieved full-text bytes keyed by content
  hash (spec section 11 Document entity: source URL, hash, access
  status), backing retrieval-order step 1 (validated local cache).

Everything is JSON + bytes on disk under ``.papercli/storage/cache`` --
no extra dependencies, Windows-safe (pathlib, atomic writes).
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "CacheEntry",
    "RetrievalCache",
    "detect_document_kind",
    "sha256_of",
    "DEFAULT_TTL",
    "NEGATIVE_TTL",
    "DOCUMENT_TTL",
]

#: Default TTLs (seconds). Negatives are intentionally short.
DEFAULT_TTL = 6 * 3600
NEGATIVE_TTL = 300
DOCUMENT_TTL = 30 * 24 * 3600  # full text is re-validated from source later


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class CacheEntry:
    """One cached metadata response (positive or negative)."""

    key: str
    status: int | None
    body: bytes
    mime: str | None
    stored_at: str                 #: UTC ISO-8601
    stored_epoch: float
    expires_at: float              #: epoch seconds
    negative: bool = False
    note: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def age(self) -> float:
        return max(0.0, time.time() - self.stored_epoch)

    def to_json(self) -> str:
        return json.dumps({
            "key": self.key,
            "status": self.status,
            "mime": self.mime,
            "negative": self.negative,
            "note": self.note,
            "stored_at": self.stored_at,
            "stored_epoch": self.stored_epoch,
            "expires_at": self.expires_at,
            "body_b64": base64.b64encode(self.body).decode("ascii"),
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "CacheEntry":
        d = json.loads(raw)
        return cls(
            key=d["key"],
            status=d.get("status"),
            body=base64.b64decode(d.get("body_b64") or ""),
            mime=d.get("mime"),
            stored_at=d.get("stored_at", ""),
            stored_epoch=float(d.get("stored_epoch", 0)),
            expires_at=float(d.get("expires_at", 0)),
            negative=bool(d.get("negative", False)),
            note=d.get("note", ""),
        )


class RetrievalCache:
    """File-backed retrieval cache with per-kind TTLs and negative entries."""

    def __init__(
        self,
        base_dir: Path | str,
        *,
        default_ttl: float = DEFAULT_TTL,
        negative_ttl: float = NEGATIVE_TTL,
        document_ttl: float = DOCUMENT_TTL,
    ):
        self.base_dir = Path(base_dir)
        self.default_ttl = default_ttl
        self.negative_ttl = negative_ttl
        self.document_ttl = document_ttl
        self._meta = self.base_dir / "meta"
        self._docs = self.base_dir / "documents"
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # -- URL -> digest helpers ----------------------------------------------
    @staticmethod
    def _digest_of(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    # -- metadata store -----------------------------------------------------
    def _meta_path(self, url: str) -> Path:
        return self._meta / f"{self._digest_of(url)}.json"

    def get(self, url: str, *, ttl: float | None = None) -> "HttpResponse | None":
        """Return a cached *positive* response for ``url`` or None.

        Negative entries are deliberately NOT served here (a remembered
        404/429 is not a response body); use :meth:`peek` to inspect them.
        """
        entry = self.peek(url)
        if entry is None or entry.negative:
            self.misses += 1
            return None
        if ttl is not None and entry.age() > ttl:
            self.misses += 1
            return None
        from papercli.providers.http_layer import HttpResponse

        self.hits += 1
        return HttpResponse(
            url=url,
            status_code=entry.status or 200,
            headers={"content-type": entry.mime or ""},
            content=entry.body,
            request_url=url,
            fetched_at=entry.stored_at,
            from_cache=True,
        )

    def peek(self, url: str) -> CacheEntry | None:
        """Return a non-expired entry (positive OR negative) for ``url``."""
        entry = self._read_entry(self._meta_path(url))
        if entry is None or entry.expired:
            return None
        return entry

    def store(
        self,
        url: str,
        status: int,
        body: bytes,
        *,
        mime: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Remember a successful response with a configurable TTL."""
        self._write(CacheEntry(
            key=url, status=status, body=body, mime=mime,
            stored_at=_now_iso(), stored_epoch=time.time(),
            expires_at=time.time() + (ttl if ttl is not None else self.default_ttl),
        ))

    def store_negative(
        self,
        url: str,
        status: int | None = None,
        note: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Remember an unavailable/error result with the (shorter) TTL."""
        self._write(CacheEntry(
            key=url, status=status, body=b"", mime=None,
            stored_at=_now_iso(), stored_epoch=time.time(),
            expires_at=time.time() + (ttl if ttl is not None else self.negative_ttl),
            negative=True, note=note or "",
        ))

    def has_negative(self, url: str) -> bool:
        """True when a non-expired negative entry exists for ``url``."""
        entry = self._read_entry(self._meta_path(url))
        return entry is not None and entry.negative and not entry.expired

    def _read_entry(self, path: Path) -> CacheEntry | None:
        if not path.exists():
            return None
        try:
            return CacheEntry.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def _write(self, entry: CacheEntry) -> None:
        self._meta.mkdir(parents=True, exist_ok=True)
        path = self._meta_path(entry.key)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            tmp.write_text(entry.to_json(), encoding="utf-8")
            tmp.replace(path)  # atomic on the same volume (Windows + POSIX)

    # -- document store ------------------------------------------------------
    def document_key(self, content: bytes) -> str:
        return sha256_of(content)

    def store_document(
        self,
        url: str,
        content: bytes,
        *,
        doc_kind: str = "",
        ttl: float | None = None,
    ) -> str:
        """Store validated full-text bytes; return the content hash."""
        digest = sha256_of(content)
        self._docs.mkdir(parents=True, exist_ok=True)
        blob = self._docs / digest
        if not blob.exists():
            blob.write_bytes(content)
        manifest = self._docs / f"{digest}.json"
        manifest.write_text(json.dumps({
            "url": url,
            "sha256": digest,
            "size": len(content),
            "kind": doc_kind,
            "stored_at": _now_iso(),
            "expires_at": time.time() + (ttl if ttl is not None else self.document_ttl),
        }), encoding="utf-8")
        # URL -> hash link for retrieval-order step 1.
        self._meta.mkdir(parents=True, exist_ok=True)
        link = self._meta / f"doclink-{self._digest_of(url)}.json"
        link.write_text(json.dumps({
            "url": url, "sha256": digest, "at": _now_iso(),
        }), encoding="utf-8")
        return digest

    def cached_document(self, url: str, *, max_age: float | None = None) -> dict[str, Any] | None:
        """Step 1 of the retrieval order: a validated cached document for URL.

        Returns ``{"path", "sha256", "kind", "size", "source_url"}`` when a
        fresh cached copy exists, else None.
        """
        link = self._meta / f"doclink-{self._digest_of(url)}.json"
        if not link.exists():
            return None
        entry = json.loads(link.read_text(encoding="utf-8"))
        digest = entry["sha256"]
        manifest = self._docs / f"{digest}.json"
        blob = self._docs / digest
        if not manifest.exists() or not blob.exists():
            return None
        man = json.loads(manifest.read_text(encoding="utf-8"))
        if time.time() > man["expires_at"]:
            return None
        if max_age is not None and man["expires_at"] - man["stored_at"] > max_age:
            return None
        return {
            "path": str(blob),
            "sha256": digest,
            "kind": man.get("kind", ""),
            "size": man.get("size", 0),
            "source_url": url,
        }


# ---------------------------------------------------------------------------
# Content validation: a link is not a retrieved report
# ---------------------------------------------------------------------------

_PDF_MAGIC = b"%PDF-"


def detect_document_kind(content: bytes, mime: str | None = None) -> str:
    """Classify retrieved bytes as an actual usable full text.

    * ``"pdf"``  -- real PDF magic (never trust the extension or MIME alone).
    * ``"xml"``  -- XML payloads (Atom feeds, JATS full text, etc.).
    * ``"html"`` -- HTML pages (publisher landing / HTML full text).
    * ``"text"`` -- plain-text / JSON payloads.
    * ``"unknown"`` -- nothing recognizable (junk, truncated body).

    Handles MIME inconsistencies: a ``Content-Type: application/pdf`` body
    that is really HTML is reported as HTML, not PDF.
    """
    if not content:
        return "unknown"
    if content[:5] == _PDF_MAGIC:
        return "pdf"
    sample = content.lstrip(b"\xef\xbb\xbf")[:4096].lower()
    if sample.lstrip().startswith(b"<?xml") or sample.lstrip().startswith(b"<arxiv ") or sample.lstrip().startswith(b"<feed"):
        return "xml"
    if sample.lstrip()[:1] == b"{" and b'"' in sample:
        return "text"
    if b"<!doctype" in sample or b"<html" in sample:
        return "html"
    if mime:
        mt = mime.lower()
        if "xml" in mt:
            return "xml"
        if "html" in mt:
            return "html"
    return "unknown"

