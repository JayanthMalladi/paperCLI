
"""Run summary builder (Phase 6, spec sections 12/14/19).

``run-summary.json`` is a STATUS SURFACE built in Phase 6 -- one of the
output-bundle files (spec section 14). The rendered review.md / review.pdf /
review.docx are Phase 7; when the export command is invoked now, the
requested rendered formats are recorded as DEFERRED in the artifact
inventory, never silently missing (spec section 14: "no silent missing
'completed' artifacts").

The summary carries (spec section 14):
* run state + limits + usage (dollar figures are ESTIMATES unless
  authoritative provider usage exists; an unknown price is never treated as
  free -- spec section 12);
* discovered/deduplicated/screened/retrieved/read/cited counts
  (spec section 19 -- reported honestly, no universal-accuracy promise);
* failures + per-branch completeness;
* the artifact inventory;
* software/model versions + search dates (reproducibility metadata).

A failed/partial run produces an HONEST partial summary, never "completed".
Secrets: the model endpoint is visible (spec section 16), the API key only
as its environment-variable reference name -- never its value.
"""

from __future__ import annotations

import json
import sys
from datetime import timezone
from pathlib import Path
from typing import Any, Mapping

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "DEFAULT_LIMITS",
    "build_run_summary",
    "write_run_summary",
    "run_dir",
    "write_structured_dumps",
]

#: Documented example ceilings (spec section 7) -- defaults for missing
#: keys so the summary always carries the full limit set.
DEFAULT_LIMITS: dict[str, Any] = {
    "wall_minutes": 360,
    "max_search_queries": 30,
    "max_discovered_records": 1000,
    "max_full_text_papers": 60,
    "max_model_calls": 300,
    "max_input_tokens": 1000000,
    "max_output_tokens": 150000,
    "max_estimated_cost_usd": None,
}


def run_dir(store: PaperStore, run_id: str) -> Path:
    """The run's artifact directory (under the storage parent)."""
    return Path(store.db_path).parent / "runs" / run_id


def _limits_block(stored: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_LIMITS)
    for k, v in (stored or {}).items():
        if v is not None:
            merged[k] = v
    return merged


def _usage_block(calls: list[dict[str, Any]], searches: list[dict[str, Any]],
                 papers: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, Any]:
    billed = [c for c in calls if c.get("outcome") in ("success", "failed")]
    cost_known = [float(c["estimated_cost_usd"]) for c in calls
                  if c.get("estimated_cost_usd") is not None]
    full_text = [d for d in docs if d.get("access_level") == "full_text"]
    # A discovered record is NEVER counted as full text read (spec section 14).
    return {
        "search_queries": sum(1 for s in searches
                              if not str(s.get("query") or "").startswith("expand:")),
        "discovered_records": len(papers),
        "full_text_papers": len(full_text),
        "model_calls": len(billed),
        "input_tokens": sum(int(c.get("input_tokens") or 0) for c in calls),
        "output_tokens": sum(int(c.get("output_tokens") or 0) for c in calls),
        "estimated_cost_usd": round(sum(cost_known), 9) if cost_known else None,
        "pricing_known": bool(cost_known),
        "cost_note": ("dollar figure is an ESTIMATE from the known price "
                      "table unless authoritative provider usage was "
                      "recorded (spec section 12); an unknown model price "
                      "is not treated as free"),
    }


def _counts_block(store: PaperStore, run_id: str,
                  papers: list[dict[str, Any]]) -> dict[str, Any]:
    conn = store._conn
    dedup_links = conn.execute(
        "SELECT COUNT(*) c FROM paper_links WHERE run_id=? AND relation='duplicate'",
        (run_id,)).fetchone()["c"]
    screened_rows = conn.execute(
        "SELECT COUNT(*) c FROM screenings s JOIN papers p ON p.id=s.paper_id "
        "WHERE p.run_id=? AND s.stage='title_abstract'", (run_id,)).fetchone()["c"]
    decided = conn.execute(
        "SELECT COUNT(*) c FROM screenings s JOIN papers p ON p.id=s.paper_id "
        "WHERE p.run_id=? AND s.stage='title_abstract' "
        "AND s.decision IN ('include','exclude','uncertain')",
        (run_id,)).fetchone()["c"]
    retrieved = conn.execute(
        "SELECT COUNT(*) c FROM documents d JOIN papers p ON p.id=d.paper_id "
        "WHERE p.run_id=? AND d.access_status IN "
        "('retrieved_full_text','retrieved_html_landing')",
        (run_id,)).fetchone()["c"]
    read_full = conn.execute(
        "SELECT COUNT(*) c FROM documents d JOIN papers p ON p.id=d.paper_id "
        "WHERE p.run_id=? AND d.access_level='full_text'",
        (run_id,)).fetchone()["c"]
    read_partial = conn.execute(
        "SELECT COUNT(*) c FROM documents d JOIN papers p ON p.id=d.paper_id "
        "WHERE p.run_id=? AND d.access_level='partial_full_text'",
        (run_id,)).fetchone()["c"]
    claims = store.claims_for_run(run_id)
    cited_keys: set[str] = set()
    for c in claims:
        cited_keys.update(c.get("citation_keys") or [])
    evidence_rows = conn.execute(
        "SELECT COUNT(*) c FROM evidence e JOIN papers p ON p.id=e.paper_id "
        "WHERE p.run_id=?", (run_id,)).fetchone()["c"]
    return {
        "discovered": len(papers),
        "deduplicated": int(dedup_links),
        "screened": int(screened_rows),
        "screened_decided": int(decided),
        "retrieved": int(retrieved),
        "read": int(read_full) + int(read_partial),
        "read_partial": int(read_partial),
        "cited": len(cited_keys),
        "claims_total": len(claims),
        "evidence_records": int(evidence_rows),
        "note": ("counts are honest tallies of the saved registries -- a "
                 "discovered record is not a full-text read, and no "
                 "exhaustive-coverage claim is made (spec sections 14/19)"),
    }


def _failures_block(tasks: list[Mapping[str, Any]],
                    counters: Mapping[str, Any]) -> list[dict[str, Any]]:
    # A "failure" for an honest summary is any task that has failed an attempt:
    # 'failed' (terminal, attempts exhausted) or 'retry_wait' (a failed attempt
    # that will be retried). Both carry a last_error and must be surfaced --
    # never present a run with in-flight failures as clean.
    failures: list[dict[str, Any]] = []
    for t in tasks:
        if t.get("state") in ("failed", "retry_wait"):
            failures.append({
                "kind": "task",
                "task_id": t.get("task_id"),
                "type": t.get("type"),
                "state": t.get("state"),
                "attempts": t.get("attempt"),
                "last_error": t.get("last_error"),
            })
    worker_state = (counters or {}).get("worker") or {}
    if worker_state.get("deadline_reached"):
        failures.append({"kind": "run",
                         "reason": "wall-clock deadline reached; dispatch "
                                    "stopped and the closure slice was "
                                    "reserved (spec section 12)"})
    if worker_state.get("budget_exhausted"):
        failures.append({"kind": "run",
                         "reason": "model budget exhausted; new dispatch "
                                    "stopped, closure slice reserved "
                                    "(spec section 12)"})
    return failures


def _branches_block(store: PaperStore, run_id: str) -> list[dict[str, Any]]:
    """Per-branch completeness (search + expansion rows, spec section 19)."""
    rows = [dict(r) for r in store._conn.execute(
        "SELECT * FROM searches WHERE run_id=? ORDER BY executed_at",
        (run_id,)).fetchall()]
    out = []
    for r in rows:
        f = json.loads(r.get("filters_json") or "{}")
        entry = {
            "id": r["id"],
            "provider": r.get("provider"),
            "kind": f.get("kind", "search"),
            "query": r.get("query"),
            "result_count": r.get("result_count"),
            "completeness": r.get("completeness"),
            "executed_at": r.get("executed_at"),
        }
        if r.get("error"):
            entry["error"] = r["error"]
        out.append(entry)
    return out


def _endpoint_used(calls: list[dict[str, Any]]) -> str | None:
    """The endpoint the run's model calls actually went to (visible, spec 16)."""
    seen: list[str] = []
    for c in calls:
        ep = c.get("endpoint")
        if ep and ep not in seen:
            seen.append(str(ep))
    return seen[0] if seen else None


def _model_used(calls: list[dict[str, Any]]) -> str | None:
    for c in calls:
        if c.get("model"):
            return str(c["model"])
    return None


def _search_dates(store: PaperStore, run_id: str) -> dict[str, str | None]:
    row = store._conn.execute(
        "SELECT MIN(executed_at) lo, MAX(executed_at) hi FROM searches "
        "WHERE run_id=? AND executed_at IS NOT NULL", (run_id,)).fetchone()
    return {"from": row["lo"], "to": row["hi"]}


def _model_info(cfg_ref: str, db_path: Path,
                endpoint_used: str | None = None,
                model_used: str | None = None) -> dict[str, Any]:
    """Software/model reproducibility metadata (spec section 14).

    The endpoint is VISIBLE; the API key only as its env-var reference name.
    ``endpoint_used`` / ``model_used`` come from the run's persisted
    model-call rows (the actual endpoint the calls went to).
    """
    from papercli import __version__ as papercli_version
    out: dict[str, Any] = {
        "software": {"package": "agent-papercli", "version": papercli_version,
                     "python": sys.version.split()[0]},
        "model": None,
    }
    snap = None
    if cfg_ref:
        p = db_path.parent / cfg_ref
        if p.exists():
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                snap = None
    if snap:
        eff = snap.get("effective") or {}
        model = eff.get("model") or {}
        refs = snap.get("secret_refs") or {}
        key_refs = [k for k in refs if "key" in str(k).lower()]
        out["model"] = {
            "adapter": model.get("adapter"),
            "name": model.get("model"),
            "endpoint": model.get("base_url") or None,
            "api_key_env": model.get("api_key_env"),
            "price_table_version": "prices-v1",
        }
    if model_used or endpoint_used:
        out["model_used"] = {
            "model": model_used,
            "endpoint": endpoint_used,
            "note": "the endpoint the model calls actually used; the API "
                    "key is stored only as its env-var reference name "
                    "(spec section 16)",
        }
    out["config_snapshot_ref"] = cfg_ref or None
    return out


def build_run_summary(store: PaperStore, run_id: str,
                      requested_formats: list[str] | None = None) -> dict[str, Any]:
    """Build the run-summary.json body for ``run_id``.

    The artifact inventory carries the recorded artifact rows + every
    bundle file the export orchestration reported (written, or missing
    with an explicit reason -- spec section 14). ``requested_formats``
    remains an accepted parameter for the CLI surface; inventory entries
    are driven by the export's own report, never a guess.
    """
    row = store.run_row(run_id)
    if row is None:
        raise KeyError(f"run not found: {run_id}")
    counters = json.loads(row.get("counters_json") or "{}")
    worker_state = counters.get("worker") or {}
    limits = _limits_block(json.loads(row.get("limits_json") or "{}"))
    calls = store.model_calls_for_run(run_id)
    papers = store.papers_for_run(run_id)
    searches = store.searches_for_run(run_id)
    docs_rows = store._conn.execute(
        "SELECT d.* FROM documents d JOIN papers p ON p.id=d.paper_id "
        "WHERE p.run_id=?", (run_id,)).fetchall()
    docs = [dict(d) for d in docs_rows]
    from papercli.jobs.tasks import TaskQueue
    queue = TaskQueue(store)
    tasks = [t.to_dict() for t in queue.list(run_id)]
    # Surface every failed-or-retrying task (the failures block itself decides
    # which states count as a failure -- failed AND retry_wait), so an in-flight
    # retry is never hidden from the honest summary.
    failures = _failures_block(tasks, worker_state)

    # Budget block: restore hard ceilings + actual usage; the deficit/exhausted
    # flags come from the worker's checkpointed budget snapshot when present
    # (never silently re-derived after the fact).
    used_calls = len([c for c in calls if c.get("outcome") in ("success", "failed")])
    used_in = sum(int(c.get("input_tokens") or 0) for c in calls)
    used_out = sum(int(c.get("output_tokens") or 0) for c in calls)
    exhausted = bool(worker_state.get("budget", {}).get("exhausted")) or any(
        limits.get(f"max_{k}") is not None and u >= limits[f"max_{k}"]
        for k, u in (("model_calls", used_calls),
                     ("input_tokens", used_in),
                     ("output_tokens", used_out)))
    budget_snapshot = worker_state.get("budget") or {}
    budget_deficit = bool(budget_snapshot.get("budget_deficit")
                          or worker_state.get("budget_deficit"))
    budget = {
        "limits": {
            "max_model_calls": limits.get("max_model_calls"),
            "max_input_tokens": limits.get("max_input_tokens"),
            "max_output_tokens": limits.get("max_output_tokens"),
            "max_estimated_cost_usd": limits.get("max_estimated_cost_usd"),
        },
        "used": {"model_calls": used_calls,
                  "input_tokens": used_in,
                  "output_tokens": used_out},
        "exhausted": exhausted,
        "partial_synthesis_reserved": bool(
            budget_snapshot.get("partial_synthesis_reserved")),
        "budget_deficit": bool(budget_deficit),
        "note": ("hard ceilings enforced via reservations before dispatch "
                 "(spec section 12); a deficit means usage exceeded its "
                 "reservation and was billed against the pool, disclosed "
                 "here -- never silently absorbed"),
    }

    # Artifact inventory: stored artifact rows + the run-summary itself +
    # the explicit statuses of every bundle file produced by the export
    # (``exported_artifacts`` -- set by the Phase-7 export orchestration;
    # without it the inventory carries only the recorded artifact rows).
    # No format is ever silently missing: a renderer that cannot run is
    # recorded missing WITH a reason (spec section 14).
    artifacts = []
    for a in store._conn.execute(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY id",
            (run_id,)).fetchall():
        a = dict(a)  # sqlite3.Row has no .get()
        artifacts.append({
            "id": a["id"], "type": a["type"], "path": a["path"],
            "hash": a.get("content_hash"),
            "run_revision": a.get("run_revision"),
            "generation_status": a.get("generation_status") or "missing",
        })
    base_dir = run_dir(store, run_id)
    summary_path = base_dir / "run-summary.json"
    artifacts.append({
        "type": "run_summary",
        "path": str(summary_path),
        "hash": None,
        "generation_status": "complete" if row["state"] in ("completed",) else "partial",
    })
    exported = getattr(store, "_phase7_exported_artifacts", None)
    if exported:
        # The export orchestration records each spec-14 file it wrote or
        # explicitly marked missing (renderers with a missing dependency
        # never destroy the others -- spec section 14).
        for name, status in exported.items():
            if isinstance(status, Mapping):
                entry = {
                    "type": name,
                    "path": status.get("path"),
                    "hash": None,
                    "generation_status": status.get("generation_status")
                                         or "missing",
                }
                if status.get("note"):
                    entry["note"] = status["note"]
            else:
                entry = {
                    "type": name,
                    "path": str(base_dir / name),
                    "hash": None,
                    "generation_status": str(status),
                }
            artifacts.append(entry)

    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "state": row.get("state"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "protocol_revision": row.get("protocol_revision"),
        "protocol_hash": row.get("protocol_hash"),
        "limits": limits,
        "usage": _usage_block(calls, searches, papers, docs),
        "counts": _counts_block(store, run_id, papers),
        "budget": budget,
        "failures": failures,
        "task_states": [
            {"task_id": t["task_id"], "type": t["type"],
             "state": t["state"], "attempt": t.get("attempt")}
            for t in tasks],
        "branches": _branches_block(store, run_id),
        "artifacts": artifacts,
        "reproducibility": {
            **_model_info(row.get("config_snapshot_ref"),
                          Path(store.db_path),
                          endpoint_used=_endpoint_used(calls),
                          model_used=_model_used(calls)),
            "search_dates": _search_dates(store, run_id),
            "generated_at": utc_now_iso(),
        },
        "notes": [
            "a failed or partial run is reported as such, never "
            "'completed' (spec section 6)",
            "one canonical structured draft/evidence registry feeds "
            "every renderer; a renderer failure marks only its own "
            "artifact missing, with a reason -- the Markdown, evidence, "
            "and reference files are never destroyed (spec section 14)",
        ],
    }
    return summary


def write_run_summary(store: PaperStore, run_id: str,
                      summary: Mapping[str, Any],
                      path: Path | str | None = None) -> Path:
    """Write the run-summary.json + record the artifact row."""
    p = Path(path) if path else run_dir(store, run_id) / "run-summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = dict(summary)
    body["schema_version"] = "1.0"
    p.write_text(json.dumps(body, indent=2, ensure_ascii=False,
                            default=str), encoding="utf-8")
    state = body.get("state")
    status = "complete" if state == "completed" else "partial"
    store.record_artifact(
        run_id, type_="run_summary", path=str(p),
        content_hash=None,
        run_revision=str(body.get("protocol_revision")),
        generation_status=status,
    )
    return p


def write_structured_dumps(store: PaperStore, run_id: str,
                           out_dir: Path | str | None = None,
                           formats: list[str] | None = None,
                           renderer_env: Any | None = None) -> dict[str, Any]:
    """The export surface: canonical structured dumps + the Phase-7
    rendered bundle.

    Structured dumps (``evidence.json``, ``claims.json``,
    ``papers-registry.json``, ``screening.json``, ``search-log.jsonl``,
    ``evidence.csv``, ``screening.csv``, ``run-summary.json``) + the
    rendered formats (``review.md``, ``references.bib``,
    ``references.csl.json``, ``review.pdf``, ``review.docx``), the
    disclosure files (``coverage-and-limitations.md``,
    ``citation-audit.json``), and the honest per-file artifact inventory
    (a missing renderer is recorded with a reason -- never a silent
    missing "completed" artifact, spec section 14).

    ``renderer_env`` (optional) is the preflight result handed to the
    Phase-7 renderers; None -> preflight this process
    (``make_renderer_env``). Tests use a bare env to stay
    deterministic; the CLI passes the real one.
    """
    base = Path(out_dir) if out_dir else run_dir(store, run_id)
    base.mkdir(parents=True, exist_ok=True)
    row = store.run_row(run_id)
    written: dict[str, str] = {}

    def dump(name: str, obj: Any) -> None:
        p = base / name
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False,
                                default=str), encoding="utf-8")
        written[name] = str(p)

    dump("evidence.json", {"run_id": run_id,
                           "records": store.evidence_for_run(run_id)})
    dump("claims.json", {"run_id": run_id,
                         "claims": store.claims_for_run(run_id)})
    from papercli.exports.renderers import build_references_csl
    dump("references.csl.json", build_references_csl(store, run_id))
    dump("papers-registry.json",
         {"run_id": run_id, "papers": store.papers_for_run(run_id)})
    screenings = store.screenings_for_run(run_id)
    dump("screening.json", {"run_id": run_id, "decisions": screenings})

    # search-log.jsonl: one envelope per line (spec section 6 batch form).
    searches = store.searches_for_run(run_id)
    lines = []
    for s in searches:
        envelope = {
            "schema_version": "1.0",
            "status": "ok",
            "data": {
                "run_id": run_id,
                "search": {
                    "id": s.get("id"),
                    "provider": s.get("provider"),
                    "query": s.get("query"),
                    "filters": json.loads(s.get("filters_json") or "{}"),
                    "executed_at": s.get("executed_at"),
                    "cursor": s.get("cursor"),
                    "result_count": s.get("result_count"),
                    "completeness": s.get("completeness"),
                    "error": s.get("error"),
                },
                "warnings": [],
            },
        }
        lines.append(json.dumps(envelope, ensure_ascii=False))
    (base / "search-log.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    written["search-log.jsonl"] = str(base / "search-log.jsonl")

    # CSV dumps (evidence + screening) -- flat, renderer-agnostic.
    import csv
    with (base / "evidence.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "paper_id", "document_id", "access_level",
                    "finding", "claim_type", "location"])
        for e in store.evidence_for_run(run_id):
            w.writerow([run_id, e.get("paper_id"), e.get("document_id"),
                        e.get("access_level"), e.get("finding"),
                        e.get("claim_type"),
                        json.dumps(e.get("location") or {}, default=str)])
    with (base / "screening.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "paper_id", "stage", "decision", "criteria",
                    "rationale", "reviewer"])
        for s in screenings:
            w.writerow([run_id, s.get("paper_id"), s.get("stage"),
                        s.get("decision"),
                        json.dumps(s.get("criterion_ids") or [], default=str),
                        s.get("rationale"), s.get("reviewer")])
    written["evidence.csv"] = str(base / "evidence.csv")
    written["screening.csv"] = str(base / "screening.csv")

    requested = [f.strip().lower() for f in (formats or []) if f.strip()]

    # Phase 7: render the requested formats from the canonical review.md +
    # registry. A renderer whose dependency is missing marks ONLY its own
    # artifact missing, with an explicit reason (spec section 14: no
    # silent missing "completed" artifacts).
    from papercli.exports import renderers
    rendered = renderers.export_bundle(
        store, run_id, out_dir=base,
        formats=["md", "pdf", "docx", "bib", "csl", "csv"]
        if not requested else requested,
        env=renderer_env)
    for name, status in rendered.items():
        if name not in written and status["generation_status"] == "complete":
            written[name] = status["path"]
    # The export's per-file statuses feed the run-summary artifact
    # inventory (written files + missing-with-reason renderers).
    structured_names = set(written)
    store._phase7_exported_artifacts = {
        name: status for name, status in rendered.items()
    } | {name: {"path": written[name], "generation_status": "complete"}
         for name in structured_names}
    summary = build_run_summary(store, run_id, requested_formats=requested)
    sp = write_run_summary(store, run_id, summary, base / "run-summary.json")
    written["run-summary.json"] = str(sp)
    return {
        "run_id": run_id,
        "written": written,
        "rendered": rendered,
        "state": row.get("state") if row else None,
    }
