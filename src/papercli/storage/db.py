"""SQLite storage access (Phase 2 additions on the Phase-1 migration runner).

Opens ``.papercli/storage/papercli.db`` (creating + migrating it lazily) and
persists the entities the provider/search work produces (spec section 11):

* **Search** -- provider, query, date, cursor, result count, completeness
  flag. The completeness flag MUST stay false/incomplete whenever a
  provider failed; a provider error is never a complete search.
* **Paper** -- canonical ids (doi/arxiv/s2/openalex), title, year,
  metadata provenance.
* **Document** -- source URL, content hash, version, access status, and the
  raw-bytes path on disk.

The Phase-1 runner and migrations are used unchanged; this module adds
only the single-writer access helpers the CLI needs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from papercli.storage.migrations import apply_migrations

__all__ = [
    "PaperStore",
    "default_db_path",
    "utc_now_iso",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_db_path(base_dir: Path | str = ".papercli") -> Path:
    return Path(base_dir) / "storage" / "papercli.db"


class PaperStore:
    """A single-writer SQLite handle for one PaperCLI storage dir."""

    def __init__(self, db_path: Path | str | None = None, *, base_dir: Path | str = ".papercli"):
        self.db_path = Path(db_path) if db_path is not None else default_db_path(base_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        apply_migrations(self.db_path)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The single-writer connection (spec section 11 strategy).

        Phase 3+ layers (task queue, evidence index, document parse quality)
        share this one handle so every write stays in the same process and the
        SQLite single-writer guarantee holds.
        """
        return self._conn

    def __enter__(self) -> "PaperStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- id helpers -----------------------------------------------------------
    @staticmethod
    def new_id(prefix: str = "run") -> str:
        import uuid
        return f"{prefix}-{uuid.uuid4()}"

    # -- runs -----------------------------------------------------------------
    def create_run(self, limits: dict[str, Any] | None = None,
                   protocol_hash: str = "", config_snapshot_ref: str = "") -> str:
        rid = self.new_id("run")
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (id, protocol_hash, config_snapshot_ref, state, "
                "started_at, limits_json, counters_json) VALUES (?,?,?,?,?,?,?)",
                (rid, protocol_hash, config_snapshot_ref, "running",
                 utc_now_iso(), json.dumps(limits or {}), "{}"),
            )
        return rid

    # -- searches ---------------------------------------------------------------
    def record_search(
        self,
        run_id: str,
        provider: str,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
        result_count: int | None = None,
        completeness: str = "complete",
        error: str | None = None,
    ) -> str:
        """Persist one Search row.

        ``completeness`` is the caller's verdict; this layer enforces the
        spec rule that an errored provider can never be stored as a
        complete search (defence in depth behind the adapters).
        """
        if error and completeness in ("complete", "full"):
            completeness = "incomplete"
        sid = self.new_id("search")
        with self._conn:
            self._conn.execute(
                "INSERT INTO searches (id, run_id, provider, query, filters_json, "
                "executed_at, cursor, result_count, completeness) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, run_id, provider, query, json.dumps(filters or {}),
                 utc_now_iso(), cursor, result_count, completeness),
            )
        return sid

    # -- papers -----------------------------------------------------------------
    def upsert_paper(
        self,
        run_id: str,
        canonical_ids: dict[str, str],
        *,
        title: str | None = None,
        authors: list[str] | None = None,
        year: int | None = None,
        metadata_provenance: dict[str, Any] | None = None,
    ) -> str:
        """Insert (or update) one Paper keyed by its canonical ids."""
        key = self._paper_key(canonical_ids)
        row = self._conn.execute(
            "SELECT id FROM papers WHERE run_id = ? AND canonical_ids_json = ?",
            (run_id, json.dumps(canonical_ids)),
        ).fetchone()
        if row is not None:
            pid = row["id"]
            with self._conn:
                self._conn.execute(
                    "UPDATE papers SET title = COALESCE(?, title), "
                    "authors_json = COALESCE(?, authors_json), "
                    "year = COALESCE(?, year), metadata_provenance_json = ? "
                    "WHERE id = ?",
                    (title,
                     json.dumps(authors) if authors is not None else None,
                     year,
                     json.dumps(metadata_provenance or {}),
                     pid),
                )
            return pid
        pid = self.new_id("paper")
        with self._conn:
            self._conn.execute(
                "INSERT INTO papers (id, run_id, canonical_ids_json, title, "
                "authors_json, year, metadata_provenance_json) VALUES (?,?,?,?,?,?,?)",
                (pid, run_id, json.dumps(canonical_ids), title,
                 json.dumps(authors) if authors is not None else None,
                 year, json.dumps(metadata_provenance or {})),
            )
        return pid

    @staticmethod
    def _paper_key(canonical_ids: dict[str, str]) -> str:
        return json.dumps(canonical_ids, sort_keys=True)

    # -- documents ----------------------------------------------------------------
    def record_document(
        self,
        paper_id: str,
        *,
        source_url: str | None = None,
        content_hash: str | None = None,
        version: str | None = None,
        access_status: str,
        parse_quality: str | None = None,
        raw_path: str | None = None,
        parse_warnings: list[str] | None = None,
        page_count: int | None = None,
        scanned: bool = False,
        identity_status: str | None = None,
        identity: dict[str, Any] | None = None,
    ) -> str:
        doc_id = self.new_id("doc")
        with self._conn:
            self._conn.execute(
                "INSERT INTO documents (id, paper_id, source_url, content_hash, "
                "version, access_status, parse_quality, raw_path, "
                "parse_warnings_json, page_count, scanned_flag, identity_status, "
                "identity_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, paper_id, source_url, content_hash, version,
                 access_status, parse_quality, raw_path,
                 json.dumps(list(parse_warnings or []), ensure_ascii=False),
                 page_count, int(bool(scanned)), identity_status,
                 json.dumps(identity or {}, ensure_ascii=False)),
            )
        return doc_id

    # -- reads ------------------------------------------------------------------
    def searches_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM searches WHERE run_id = ? ORDER BY executed_at",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def papers_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


    def find_paper_by_ids(self, run_id: str, canonical_ids: dict[str, str]) -> str | None:
        """Return the paper id in ``run_id`` matching these canonical ids,
        or None. Matching is by the stored canonical_ids_json subset."""
        if not canonical_ids:
            return None
        for row in self.papers_for_run(run_id):
            stored = json.loads(row.get("canonical_ids_json") or "{}")
            if all(stored.get(k) == v for k, v in canonical_ids.items()):
                return row["id"]
        return None

    # -- documents (Phase 3 additions) --------------------------------------
    def record_document_identity(
        self,
        doc_id: str,
        *,
        parse_warnings: list[str] | None = None,
        page_count: int | None = None,
        scanned: bool | None = None,
        identity_status: str | None = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        """Fill the parse-quality + identity columns on an existing Document.

        Used after a parse/ingest has produced warnings, a page count, and a
        wrong-paper check verdict.
        """
        sets: list[str] = []
        vals: list[Any] = []
        if parse_warnings is not None:
            sets.append("parse_warnings_json = ?")
            vals.append(json.dumps(list(parse_warnings), ensure_ascii=False))
        if page_count is not None:
            sets.append("page_count = ?")
            vals.append(page_count)
        if scanned is not None:
            sets.append("scanned_flag = ?")
            vals.append(int(bool(scanned)))
        if identity_status is not None:
            sets.append("identity_status = ?")
            vals.append(identity_status)
        if identity is not None:
            sets.append("identity_json = ?")
            vals.append(json.dumps(identity, ensure_ascii=False))
        if not sets:
            return
        vals.append(doc_id)
        with self._conn:
            self._conn.execute(
                f"UPDATE documents SET {', '.join(sets)} WHERE id = ?", vals)

    def document_row(self, doc_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row is not None else None

    # -- evidence (Phase 3) ---------------------------------------------------
    def record_evidence(
        self,
        paper_id: str,
        *,
        document_id: str | None = None,
        access_level: str,
        location: dict[str, Any] | None = None,
        supporting_excerpt: str | None = None,
        finding: str,
        claim_type: str | None = None,
        methods: dict[str, Any] | None = None,
        population_or_system: str | None = None,
        limitations: list[str] | None = None,
        extraction_warnings: list[str] | None = None,
    ) -> str:
        """Persist one Evidence record (spec section 9 minimum + 0002 fields)."""
        ev_id = self.new_id("ev")
        with self._conn:
            self._conn.execute(
                "INSERT INTO evidence (id, paper_id, document_id, access_level, "
                "location_json, supporting_excerpt, finding, claim_type, "
                "methods_json, population_or_system, limitations_json, "
                "warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev_id, paper_id, document_id, access_level,
                 json.dumps(location or {}, ensure_ascii=False),
                 supporting_excerpt, finding, claim_type,
                 json.dumps(methods or {}, ensure_ascii=False),
                 population_or_system,
                 json.dumps(list(limitations or []), ensure_ascii=False),
                 json.dumps(list(extraction_warnings or []),
                            ensure_ascii=False)),
            )
        return ev_id

    def evidence_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Evidence rows for the papers of ``run_id`` (for ``papers evidence list``)."""
        rows = self._conn.execute(
            "SELECT e.* FROM evidence e JOIN papers p ON p.id = e.paper_id "
            "WHERE p.run_id = ? ORDER BY e.id", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("location_json", "methods_json", "limitations_json",
                        "warnings_json"):
                d[col.replace("_json", "")] = json.loads(d.pop(col) or "[]")
            out.append(d)
        return out

    # -- document text index (evidence location resolution) ---------------------
    def store_document_text(self, document_id: str,
                             sections: list[dict[str, Any]]) -> int:
        """Write the stable (page, section, paragraph) -> text index.

        ``sections`` are ``{"page", "section", "paragraph", "text"}``
        records; the unique key makes this safe under single-writer use.
        Returns the number of paragraphs stored.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM document_text WHERE document_id = ?", (document_id,))
            self._conn.executemany(
                "INSERT INTO document_text (document_id, page, section, "
                "paragraph, text) VALUES (?,?,?,?,?)",
                [(document_id, int(s.get("page", 1)),
                  str(s.get("section", "")),
                  int(s.get("paragraph", 0)), s.get("text", ""))
                 for s in sections],
            )
            return len(sections)

    def document_text(self, document_id: str,
                       page: int | None = None) -> list[dict[str, Any]]:
        if page is not None:
            rows = self._conn.execute(
                "SELECT * FROM document_text WHERE document_id = ? AND page = ? "
                "ORDER BY section, paragraph", (document_id, page)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM document_text WHERE document_id = ? "
                "ORDER BY page, section, paragraph", (document_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_excerpt(self, document_id: str,
                        excerpt: str) -> dict[str, Any] | None:
        """Locate an exact excerpt inside the stored source text.

        Returns the matching index row (so an evidence location resolves to
        EXACT stored text -- acceptance test 'Extraction'), or None.
        """
        if not excerpt or not excerpt.strip():
            return None
        needle = excerpt.strip()
        rows = self.document_text(document_id)
        for row in rows:
            if needle in row["text"]:
                return row
        return None

    # -- runs (Phase 3 helpers) ---------------------------------------------------
    def update_run(self, run_id: str, **fields: Any) -> None:
        """Update scalar run fields (state, timestamps, counters, limits)."""
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE runs SET {cols} WHERE id = ?",
                [*fields.values(), run_id])

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

