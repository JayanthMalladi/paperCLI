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
    ) -> str:
        doc_id = self.new_id("doc")
        with self._conn:
            self._conn.execute(
                "INSERT INTO documents (id, paper_id, source_url, content_hash, "
                "version, access_status, parse_quality, raw_path) VALUES (?,?,?,?,?,?,?,?)",
                (doc_id, paper_id, source_url, content_hash, version,
                 access_status, parse_quality, raw_path),
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
