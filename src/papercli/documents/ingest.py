"""Local document ingestion (spec section 8 retrieval order step 1; CLI `papers ingest`).

Import a local PDF/HTML/XML into a run's Document registry, reusing the
Phase-2 document persistence (raw bytes + content hash + source + access
status -- no duplicate storage paths) and adding the Phase-3 parse-quality
and identity columns.

Flow:

  1. Read the file bytes; detect kind (pdf/xml/html) -- unknown kind is a
     rejected ingest, not a silent accept.
  2. Parse (PDF in an isolated child process, spec section 16) and store
     the stable text index in ``document_text``.
  3. Run the wrong-paper guard when the run context supplies a paper id
     (a definite mismatch REJECTS; an inconclusive check is ingested but
     flagged -- never silently accepted).
  4. Persist the Document row through the Phase-2 ``record_document``
     accessor plus the Phase-3 parse-quality/identity columns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from papercli.documents.identity import check_identity
from papercli.documents.parser import ParseError, parse_document
from papercli.providers.cache import detect_document_kind, sha256_of
from papercli.storage.db import PaperStore

__all__ = [
    "IngestRejected",
    "ingest_local_document",
    "parse_document_bytes",
]

#: Access status for user-imported local documents (retrieval order step 1:
#: "validated local document cache or user-imported document").
LOCAL_ACCESS_STATUS = "retrieved_full_text"


class IngestRejected(Exception):
    """An ingestion was rejected (unknown kind, parse failure, or a
    definite wrong-paper identity mismatch)."""

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def parse_document_bytes(data: bytes, kind: str | None = None, *,
                         timeout: float | None = None,
                         max_bytes: int | None = None) -> dict[str, Any]:
    """Parse local document bytes into the Phase-3 report shape.

    Detects the kind when not given (byte validation, not file extension);
    an undetectable payload raises :class:`IngestRejected`.
    """
    from papercli.documents.parser import DEFAULT_PARSE_TIMEOUT_S
    kd = detect_document_kind(data)
    if kind is None:
        kind = kd
    if kind not in ("pdf", "xml", "html"):
        raise IngestRejected(
            f"unsupported document kind: {kind!r} "
            f"(detected: {kd!r}); PDF/HTML/XML only")
    try:
        return parse_document(
            data, kind,
            timeout=timeout if timeout is not None else DEFAULT_PARSE_TIMEOUT_S,
            max_bytes=max_bytes)
    except ParseError as e:
        raise IngestRejected(str(e)) from e


def ingest_local_document(
    store: PaperStore,
    run_id: str,
    path: Path | str,
    *,
    paper_id: str | None = None,
    requested_identity: dict[str, Any] | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Ingest one local file into the run's Document registry.

    ``requested_identity`` is the run context's paper identity
    (``{"doi", "title", ...}``). When a ``paper_id`` is supplied, the
    requested identity is taken from that paper's stored row unless the
    caller passed an explicit dict -- that is how the CLI's
    "run context supplies a paper id" path works.

    Returns the ingestion report::

        {"document_id", "kind", "content_hash", "access_status",
         "page_count", "parse_warnings", "scanned", "identity_status",
         "identity", "rejected": false}

    Raises :class:`IngestRejected` when the document must not enter the
    registry (unknown kind, parse failure, definite identity mismatch).
    """
    file_path = Path(path)
    if not file_path.exists():
        raise IngestRejected(f"document file not found: {file_path}")
    data = file_path.read_bytes()
    report = parse_document_bytes(data, timeout=timeout, max_bytes=max_bytes)
    kind = report["kind"]

    # Paper context: resolve the requested identity when only a paper id was
    # supplied.
    requested = dict(requested_identity or {})
    if not requested and paper_id:
        for row in store.papers_for_run(run_id):
            if row["id"] == paper_id:
                meta = json.loads(row.get("metadata_provenance_json") or "{}")
                ids = json.loads(row.get("canonical_ids_json") or "{}")
                requested = {
                    "doi": ids.get("doi") or meta.get("doi"),
                    "title": row.get("title"),
                    "arxiv_id": ids.get("arxiv_id"),
                }
                break
    requested = {k: v for k, v in requested.items() if v}

    # Wrong-paper guard: a definite mismatch rejects; inconclusive flags.
    identity_status = "skipped"
    identity_result: dict[str, Any] = {}
    if requested:
        verdict = check_identity(report["identity"], requested)
        identity_status = verdict["status"]
        identity_result = verdict
        if verdict["status"] == "unmatched":
            raise IngestRejected(
                "wrong-paper guard: document identity does not match the "
                "requested paper",
                details=verdict["details"])

    # Phase-2 persistence path (no duplicate storage): raw bytes + hash +
    # source + access status via record_document, then the Phase-3 columns.
    doc_paper_id = paper_id if paper_id else _auto_paper_id(store, run_id)
    doc_id = store.record_document(
        doc_paper_id,
        source_url=f"local:{file_path.name}",
        content_hash=sha256_of(data),
        access_status=LOCAL_ACCESS_STATUS,
        parse_quality=kind,
        raw_path=str(file_path),
        parse_warnings=report["warnings"],
        page_count=report["page_count"],
        scanned=report["scanned"],
        identity_status=identity_status,
        identity=identity_result.get("details", {}),
    )
    # Stable (page, section, paragraph) -> text index for evidence locations.
    n = store.store_document_text(doc_id, report["text_index"])

    warnings = list(report["warnings"])
    if identity_status == "inconclusive":
        warnings.append(
            "wrong-paper guard: identity could not be confirmed "
            "(embedded evidence inconclusive); document flagged")

    return {
        "document_id": doc_id,
        "paper_id": doc_paper_id,
        "kind": kind,
        "content_hash": sha256_of(data),
        "access_status": LOCAL_ACCESS_STATUS,
        "page_count": report["page_count"],
        "parse_warnings": warnings,
        "scanned": report["scanned"],
        "identity_status": identity_status,
        "identity": identity_result.get("details", {}),
        "sections_stored": n,
        "rejected": False,
    }


def _auto_paper_id(store: PaperStore, run_id: str) -> str:
    """A document must reference a Paper row (foreign key); when the caller
    pinned none, use (or create) the local-import paper for the run."""
    row = store._conn.execute(
        "SELECT id FROM papers WHERE run_id = ? AND "
        "metadata_provenance_json LIKE '%local-import%' "
        "ORDER BY id LIMIT 1", (run_id,)).fetchone()
    if row is not None:
        return row["id"]
    return store.upsert_paper(
        run_id, {}, metadata_provenance={"created_by": "local-import",
                                          "run_id": run_id})
