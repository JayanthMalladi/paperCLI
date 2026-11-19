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
        # The detached worker and the CLI share this database (two
        # processes on Windows): a short busy timeout makes a lock wait
        # instead of a 'database is locked' error (spec section 11,
        # single-writer strategy with a short critical section).
        self._conn.execute("PRAGMA busy_timeout = 5000")

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
                   protocol_hash: str = "", config_snapshot_ref: str = "",
                   state: str = "running") -> str:
        rid = self.new_id("run")
        now = utc_now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (id, protocol_hash, config_snapshot_ref, state, "
                "started_at, created_at, limits_json, counters_json, "
                "protocol_revision) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, protocol_hash, config_snapshot_ref, state,
                 now, now, json.dumps(limits or {}), "{}", 1),
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
                "executed_at, cursor, result_count, completeness, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, run_id, provider, query, json.dumps(filters or {}),
                 utc_now_iso(), cursor, result_count, completeness,
                 error),
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
        access_level: str | None = None,
    ) -> str:
        doc_id = self.new_id("doc")
        with self._conn:
            self._conn.execute(
                "INSERT INTO documents (id, paper_id, source_url, content_hash, "
                "version, access_status, parse_quality, raw_path, "
                "parse_warnings_json, page_count, scanned_flag, identity_status, "
                "identity_json, access_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, paper_id, source_url, content_hash, version,
                 access_status, parse_quality, raw_path,
                 json.dumps(list(parse_warnings or []), ensure_ascii=False),
                 page_count, int(bool(scanned)), identity_status,
                 json.dumps(identity or {}, ensure_ascii=False),
                 access_level),
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
            for col in ("location_json", "methods_json", "limitations_json"):
                d[col.replace("_json", "")] = json.loads(d.pop(col) or "{}")
            # spec section 9 minimum field name: extraction_warnings
            d["extraction_warnings"] = json.loads(d.pop("warnings_json") or "[]")
            # Published contract (agent_contract/schemas/evidence.json)
            # requires the key "evidence_id"; alias it from the stored
            # primary key so every surfaced record conforms (D-P5-0).
            d["evidence_id"] = d["id"]
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

    def find_run_by_plan_id(self, plan_id: str) -> dict[str, Any] | None:
        """The Run row whose saved plan carries ``plan_id`` (None if
        none). A plan file written by ``papers review plan`` names the run
        it created; this is how ``papers review run`` re-finds that row."""
        if not plan_id:
            return None
        for row in self._conn.execute("SELECT id, plan_json FROM runs").fetchall():
            try:
                plan = json.loads(row["plan_json"] or "{}")
            except ValueError:
                continue
            if plan.get("plan_id") == plan_id:
                return self.run_row(row["id"])
        return None

    # -- research (Phase 4) -------------------------------------------------
    def save_run_plan(self, run_id: str, plan: dict[str, Any]) -> None:
        """Store the validated protocol on a run (state stays 'planned')."""
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET plan_json = ? WHERE id = ?",
                (json.dumps(plan, ensure_ascii=False, sort_keys=True), run_id),
            )

    def screenings_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT s.* FROM screenings s JOIN papers p ON p.id = s.paper_id "
            "WHERE p.run_id = ? ORDER BY s.decided_at, s.id", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["criterion_ids"] = json.loads(d.pop("criteria_json") or "[]")
            out.append(d)
        return out

    def record_screening(
        self,
        paper_id: str,
        *,
        stage: str,
        decision: str,
        criterion_ids: list[str] | None = None,
        rationale: str | None = None,
        evidence_basis: str | None = None,
        reviewer: str | None = None,
        prompt_version: str | None = None,
    ) -> str:
        """Persist one screening decision (spec section 9 field set)."""
        sid = self.new_id("scr")
        with self._conn:
            self._conn.execute(
                "INSERT INTO screenings (id, paper_id, stage, decision, "
                "criteria_json, rationale, reviewer, prompt_version, "
                "evidence_basis, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, paper_id, stage, decision,
                 json.dumps(list(criterion_ids or []), ensure_ascii=False),
                 rationale, reviewer, prompt_version, evidence_basis,
                 utc_now_iso()),
            )
        return sid

    def record_uncertainty(
        self,
        run_id: str,
        paper_id: str,
        *,
        stage: str,
        reason: str,
        status: str = "queued",
    ) -> str:
        uid = self.new_id("unc")
        with self._conn:
            self._conn.execute(
                "INSERT INTO uncertainty (id, run_id, paper_id, stage, reason, "
                "status) VALUES (?,?,?,?,?,?)",
                (uid, run_id, paper_id, stage, reason, status),
            )
        return uid

    def uncertainty_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM uncertainty WHERE run_id = ? ORDER BY id",
            (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def update_uncertainty(self, uid: str, *, status: str,
                           reason: str | None = None,
                           increment_rechecks: bool = False) -> None:
        sets = ["status = ?", "updated_at = ?"]
        vals: list[Any] = [status, utc_now_iso()]
        if reason is not None:
            sets.append("reason = ?")
            vals.append(reason)
        if increment_rechecks:
            sets.append("rechecks = rechecks + 1")
        vals.append(uid)
        with self._conn:
            self._conn.execute(
                f"UPDATE uncertainty SET {', '.join(sets)} WHERE id = ?", vals)

    def set_paper_access_level(self, paper_id: str, access_level: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE papers SET access_level = ? WHERE id = ?",
                (access_level, paper_id),
            )

    def record_paper_link(
        self,
        run_id: str,
        source_paper_id: str,
        target_paper_id: str,
        *,
        relation: str,
        confidence: float,
        signals: dict[str, Any] | None = None,
    ) -> str:
        """Persist a dedup / version link (never a silent merge)."""
        lid = self.new_id("link")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_links (id, run_id, source_paper_id, "
                "target_paper_id, relation, confidence, signals_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (lid, run_id, source_paper_id, target_paper_id, relation,
                 float(confidence), json.dumps(signals or {}, ensure_ascii=False)),
            )
        return lid

    def paper_links_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM paper_links WHERE run_id = ? ORDER BY id",
            (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["signals"] = json.loads(d.pop("signals_json") or "{}")
            out.append(d)
        return out

    def record_expansion(
        self,
        run_id: str,
        seed_paper_id: str,
        *,
        provider: str,
        method: str,
        query: str,
        result: dict[str, Any],
    ) -> str:
        """Persist a bounded-search-expansion result (provenance chain).

        A provider failure during expansion is recorded with
        ``completeness='incomplete'`` -- never 'complete' (spec section 19).
        """
        sid = self.new_id("exp")
        completeness = result.get("completeness", "incomplete")
        # Spec section 19: a provider failure OR a keyless skip is never a
        # complete search. Only a branch that was fully exhausted within the
        # cap is "complete"; everything else persists as "incomplete".
        stored = "complete" if completeness == "complete" else "incomplete"
        prov = result.get("provenance") or {}
        chain = (prov.get("chain")
                 or f"seed:{seed_paper_id}->via:{provider}:{method}")
        filters = {
            "kind": "expansion",
            "seed_paper_id": seed_paper_id,
            "provider": provider,
            "method": method,
            "completeness": stored,
            "deferred": int(result.get("deferred") or 0),
            "candidates_added": int(result.get("candidates_added") or 0),
            "result_count": result.get("result_count"),
            "cursor": result.get("cursor"),
            "error": result.get("error"),
            "skip_reason": result.get("skip_reason"),
            "provenance": chain,
        }
        with self._conn:
            self._conn.execute(
                "INSERT INTO searches (id, run_id, provider, query, filters_json, "
                "executed_at, cursor, result_count, completeness) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, run_id, provider, f"expand:{provider}",
                 json.dumps(filters, ensure_ascii=False),
                 utc_now_iso(),
                 result.get("cursor"),
                 result.get("result_count"),
                 stored),
            )
        return sid

    # -- model calls + claims (Phase 5) --------------------------------------
    def record_model_call(
        self,
        run_id: str,
        *,
        model: str,
        outcome: str,
        task_type: str | None = None,
        prompt_hash: str | None = None,
        prompt_version: str | None = None,
        protocol_revision: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
        usage: dict[str, Any] | None = None,
        endpoint: str | None = None,
        api_key_env: str | None = None,
    ) -> str:
        """Persist one Model-call row (spec section 11 minimum + Phase 5
        detail). ``usage`` carries authoritative/estimated cost capture;
        a null cost + pricing_known=false records an UNKNOWN price
        (never treated as free -- spec section 12). The secret is stored
        only as its env-var reference name (spec section 13)."""
        mid = self.new_id("mcall")
        with self._conn:
            self._conn.execute(
                "INSERT INTO model_calls (id, run_id, model, prompt_hash, "
                "prompt_version, input_tokens, output_tokens, "
                "estimated_cost_usd, outcome, task_type, "
                "protocol_revision, usage_json, endpoint, api_key_env) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, run_id, model, prompt_hash, prompt_version,
                 input_tokens, output_tokens, estimated_cost_usd,
                 outcome, task_type, protocol_revision or "1",
                 json.dumps(usage or {}, ensure_ascii=False),
                 endpoint, api_key_env),
            )
        return mid

    def model_calls_for_run(self, run_id: str,
                            outcome: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM model_calls WHERE run_id = ?"
        args: list[Any] = [run_id]
        if outcome is not None:
            q += " AND outcome = ?"
            args.append(outcome)
        q += " ORDER BY id"
        rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["usage"] = json.loads(d.get("usage_json") or "{}")
            out.append(d)
        return out

    def record_claim(
        self,
        run_id: str,
        *,
        claim_text: str,
        location: str | None = None,
        evidence_ids: list[str] | None = None,
        audit_status: str = "uncertain",
        is_synthesis: bool = False,
        citation_keys: list[str] | None = None,
    ) -> str:
        """Persist one Claim-ledger row (spec section 10).

        ``citation_keys`` are the registry keys the claim cited; the
        citations audit validates them against the bibliography registry
        (fabricated keys are flagged, spec section 10)."""
        cid = self.new_id("claim")
        with self._conn:
            self._conn.execute(
                "INSERT INTO claims (id, run_id, draft_location, claim_text, "
                "evidence_ids_json, audit_status, is_synthesis, "
                "citation_keys_json) VALUES (?,?,?,?,?,?,?,?)",
                (cid, run_id, location, claim_text,
                 json.dumps(list(evidence_ids or []), ensure_ascii=False),
                 audit_status, 1 if is_synthesis else 0,
                 json.dumps(list(citation_keys or []), ensure_ascii=False)),
            )
        return cid

    def claims_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE run_id = ? ORDER BY id",
            (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence_ids"] = json.loads(d.get("evidence_ids_json") or "[]")
            d["citation_keys"] = json.loads(d.get("citation_keys_json") or "[]")
            d["is_synthesis"] = bool(d.get("is_synthesis"))
            # 0001 stores the draft location in draft_location; 0006 added
            # a nullable location column -- surface one resolved key.
            d["location"] = d.get("location") or d.get("draft_location")
            out.append(d)
        return out

    def record_artifact(
        self,
        run_id: str,
        *,
        type_: str,
        path: str,
        content_hash: str | None = None,
        run_revision: str | None = None,
        generation_status: str = "complete",
    ) -> str:
        """Persist one Artifact row (spec section 11)."""
        aid = self.new_id("art")
        with self._conn:
            self._conn.execute(
                "INSERT INTO artifacts (id, run_id, type, path, "
                "content_hash, run_revision, generation_status) "
                "VALUES (?,?,?,?,?,?,?)",
                (aid, run_id, type_, path, content_hash, run_revision,
                 generation_status),
            )
        return aid
