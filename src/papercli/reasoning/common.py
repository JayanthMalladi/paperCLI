"""Shared helpers for the model-driven reasoning tasks (Phase 5).

Each reasoning task is a pure function ``(store, task_payload) -> result``
registered as a task type on the Phase-4 TaskQueue (the detached worker,
Phase 6, dispatches them through the same lease protocol). Model-driven
tasks take the adapter as an injectable ``adapter_factory`` so the suite
stays fully offline; when no model is configured, the task reports
``skipped_no_model`` and the run continues deterministically
(spec section 4, agent-driven mode).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "TASK_TYPES",
    "PROMPT_VERSIONS",
    "build_adapter",
    "model_available",
    "run_protocol",
    "input_hash",
    "skipped_result",
    "paper_record",
    "citation_key",
    "bibliography_for_run",
]

#: Registered task types (Phase 5). Phase 6 dispatches these through the
#: Phase-4 TaskQueue lease protocol.
TASK_TYPES = ("screen_uncertain", "extract_findings", "synthesize", "critique")

#: Prompt version per task -- part of the model-call idempotency key
#: (spec section 11: protocol revision + prompt version; reusing a retrieval
#: cache must not silently reuse an obsolete interpretation).
PROMPT_VERSIONS: dict[str, str] = {
    "screen_uncertain": "screen-v1",
    "extract_findings": "extract-v1",
    "synthesize": "synthesize-v1",
    "critique": "critique-v1",
}


def build_adapter(cfg: Any, *, limits: Mapping[str, Any] | None = None,
                  store: PaperStore | None = None,
                  run_id: str | None = None,
                  adapter_factory: Callable[..., Any] | None = None):
    """Build the model adapter for a task.

    ``adapter_factory`` is the injection point for the offline suite
    (a factory returning a :class:`ModelAdapter` over an httpx
    MockTransport). Default: the real adapter, configured for the run's
    budget, with model-call rows persisted through the store.
    """
    from papercli.models.adapter import ModelAdapter
    from papercli.models.rate_limit import ModelBudget, RateLimiter

    if adapter_factory is not None:
        return adapter_factory(cfg=cfg, store=store, run_id=run_id,
                               limits=limits)
    budget = ModelBudget(dict(limits or (cfg.get("limits") or {})))
    rec_fn = None
    if store is not None and run_id is not None:
        def rec_fn(rec: Mapping[str, Any]) -> Any:
            return store.record_model_call(
                run_id,
                model=rec.get("model", ""),
                outcome=rec.get("outcome", "failed"),
                task_type=rec.get("task_type"),
                prompt_hash=rec.get("prompt_hash"),
                prompt_version=rec.get("prompt_version"),
                protocol_revision=rec.get("protocol_revision"),
                input_tokens=rec.get("input_tokens"),
                output_tokens=rec.get("output_tokens"),
                estimated_cost_usd=(rec.get("cost") or {}).get("estimated_cost_usd"),
                usage={**(rec.get("cost") or {}),
                       **(rec.get("usage") or {})},
                endpoint=rec.get("endpoint"),
                api_key_env=rec.get("api_key_env"),
            )
    return ModelAdapter.from_config(cfg, budget=budget,
                                    rate_limiter=RateLimiter(),
                                    on_record=rec_fn)


def model_available(cfg: Any) -> bool:
    """True when a model-driven task can actually dispatch.

    ``None`` / unconfigured (no base_url + model + key reference) -> False
    (the agent-driven mode, spec section 4)."""
    if cfg is None:
        return False
    base_url = str(cfg.get("model.base_url") or "").strip()
    model = str(cfg.get("model.model") or "").strip()
    if not base_url or not model:
        return False
    key_ref = str(cfg.get("model.api_key_env") or "").strip()
    if not key_ref:
        return False
    import os
    env = dict(getattr(cfg, "env", {}) or {})
    env.update(os.environ)
    return bool((env.get(key_ref) or "").strip())


def run_protocol(store: PaperStore, run_id: str) -> dict[str, Any]:
    """The saved, validated protocol on the run (the single source of
    truth for criteria, limits, themes)."""
    row = store.run_row(run_id) or {}
    plan = json.loads(row.get("plan_json") or "{}")
    return plan or {}


def input_hash(*parts: Any) -> str:
    """A stable content hash for task-input keying (idempotency input,
    spec section 11)."""
    digest = hashlib.sha256(
        json.dumps(list(parts), sort_keys=True,
                   default=str).encode("utf-8"))
    return digest.hexdigest()[:16]


def skipped_result(task_type: str, run_id: str, **extra: Any) -> dict[str, Any]:
    """The no-model task outcome: the run continues deterministically
    (agent-driven mode, spec section 4)."""
    return {"task_type": task_type, "run_id": run_id,
            "status": "skipped_no_model",
            "note": "model not configured; deterministic layers only "
                    "(spec section 4)",
            **extra}


def paper_record(store: PaperStore, paper_id: str) -> dict[str, Any]:
    """A paper row as a flat dict with parsed json fields."""
    row = store._conn.execute(
        "SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    d = dict(row)
    d["authors"] = json.loads(d.get("authors_json") or "[]")
    d["canonical_ids"] = json.loads(d.get("canonical_ids_json") or "{}")
    d["metadata_provenance"] = json.loads(d.get("metadata_provenance_json") or "{}")
    return d


def citation_key(rec: Mapping[str, Any]) -> str:
    """A STABLE bibliography citation key for a paper record.

    Deterministic: first-author surname (lowercased, alphanumeric) + year,
    ``firstauthor{year}``; the record id breaks ties. Keys are generated
    ONLY from the saved registry -- the model selects among them and can
    never fabricate an entry (spec section 10).
    """
    authors = rec.get("authors") or []
    surname = ""
    if authors:
        surname = str(authors[0]).split()[-1] if str(authors[0]).split() else ""
    surname = "".join(ch for ch in surname.lower() if ch.isalnum()) or "author"
    year = rec.get("year")
    base = f"{surname}{year}" if year else surname
    short = str(rec.get("id", ""))[-4:]
    return f"{base}{short}"


def bibliography_for_run(store: PaperStore, run_id: str) -> list[dict[str, Any]]:
    """The run's bibliography registry (spec section 10: stable citation
    keys are supplied by the registry; the model selects, never invents).

    Every entry is generated from a saved paper row (identity fields
    already validated by the Phase-3 wrong-paper check); no reference is
    ever invented here.
    """
    out = []
    for row in store.papers_for_run(run_id):
        rec = {
            "citation_key": citation_key(row),
            "paper_id": row["id"],
            "title": row.get("title"),
            "authors": json.loads(row.get("authors_json") or "[]"),
            "year": row.get("year"),
            "identifiers": json.loads(row.get("canonical_ids_json") or "{}"),
            "provenance": json.loads(row.get("metadata_provenance_json") or "{}"),
            "access_level": row.get("access_level"),
        }
        out.append(rec)
    out.sort(key=lambda r: r["citation_key"])
    return out
