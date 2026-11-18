"""Model-driven recheck of the uncertainty queue (spec section 9).

Deterministic screening (Phase 4) already produced every *confident*
decision and pushed the ambiguous records into the run's uncertainty
queue. This task is the **bounded automated recheck**: one model pass
over the queued records, each becoming a full spec-section-9 Screening
row (decision, criterion IDs, rationale, evidence basis, reviewer =
model identity, prompt version, timestamp). Leftovers the model cannot
resolve are reported **unresolved** -- they are never force-decided,
and the task is bounded to exactly one pass (max N rechecks = 1).

No model configured -> the task reports ``skipped_no_model`` and the run
continues deterministically (agent-driven mode, spec section 4).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.agent_contract.schemas import load_schema
from papercli.reasoning.common import (
    PROMPT_VERSIONS,
    build_adapter,
    input_hash,
    model_available,
    paper_record,
    run_protocol,
    skipped_result,
)
from papercli.research.screening import assign_criterion_ids
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = ["screen_uncertain"]

_TASK = "screen_uncertain"
_SCHEMA = "screening"

#: The model's screen-decision reply shape (the screening schema's decision
#: fields; paper_id/stage are added at persistence time, below).
_DECISION_SCHEMA = {
    "type": "object",
    "required": ["decision"],
    "properties": {
        "decision": {"type": "string",
                     "enum": ["include", "exclude", "uncertain"]},
        "criterion_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "evidence_basis": {"type": "string",
                            "enum": ["abstract", "full_text", "metadata",
                                      "none"]},
    },
}


def _record_text(rec: Mapping[str, Any]) -> str:
    parts = [str(rec.get("title") or ""), str(rec.get("abstract") or "")]
    parts += [str(a) for a in rec.get("authors") or []]
    return " \n ".join(parts)


def _screen_prompt(protocol: Mapping[str, Any], rec: Mapping[str, Any],
                   inc_ids: list[str], exc_ids: list[str]) -> str:
    inc = protocol.get("inclusion_criteria") or []
    exc = protocol.get("exclusion_criteria") or []
    criteria: list[str] = []
    for i, c in enumerate(inc, 1):
        criteria.append(f"{inc_ids[i-1]} (include): {c}")
    for i, c in enumerate(exc, 1):
        criteria.append(f"{exc_ids[i-1]} (exclude): {c}")
    record_payload = json.dumps(
        {"title": rec.get("title"), "authors": rec.get("authors"),
         "year": rec.get("year"), "abstract": rec.get("abstract")},
        ensure_ascii=False)
    return (
        "You are a systematic-review screening assistant. Decide one "
        "title/abstract record.\n"
        f"RESEARCH QUESTION: {protocol.get('question','')}\n"
        f"CRITERIA:\n" + "\n".join(criteria) + "\n"
        f"RECORD: {record_payload}\n"
        "Reply with a single JSON object: {\'decision\': 'include' | "
        "'exclude' | 'uncertain', \'criterion_ids\': [ids the decision "
        "rests on], \'rationale\': string, \'evidence_basis\': 'abstract' "
        "| \'metadata\'}"
    )


def screen_uncertain(store: PaperStore, task_payload: Mapping[str, Any],
                    *, cfg: Any | None = None,
                    adapter_factory=None) -> dict[str, Any]:
    """Model recheck of the run's uncertainty queue (one bounded pass)."""
    run_id = str(task_payload.get("run_id") or task_payload.get("id") or "")
    queue = [u for u in store.uncertainty_for_run(run_id)
             if u.get("status") in ("queued", None, "unresolved")]
    protocol = run_protocol(store, run_id)
    inc_ids, exc_ids = assign_criterion_ids(protocol)

    if not model_available(cfg) or cfg is None:
        return skipped_result(_TASK, run_id,
                              pending_uncertain=len(queue))

    adapter = build_adapter(cfg, limits=protocol.get("limits"),
                            store=store, run_id=run_id,
                            adapter_factory=adapter_factory)
    resolved, unresolved, records = 0, [], []
    for unc in queue:
        if unc.get("rechecks", 0) >= 1:
            # Bounded: exactly one automated recheck pass (spec section 9).
            # The leftover is reported unresolved, never force-decided,
            # and the queue row is marked so a re-run does not recheck it.
            store.update_uncertainty(unc["id"], status="unresolved",
                                     reason="recheck limit reached (max 1)",
                                     increment_rechecks=False)
            unresolved.append({"uncertainty_id": unc["id"],
                               "paper_id": unc.get("paper_id"),
                               "reason": "recheck limit reached (max 1)"})
            continue
        rec = paper_record(store, unc["paper_id"]) if unc.get("paper_id") else {}
        rec.setdefault("title", None)
        prompt = _screen_prompt(protocol, rec, inc_ids, exc_ids)
        # The model reply is a screen-decision object (the published
        # screening schema's decision fields); validate against it -- the
        # paper_id/stage wrapper is added when the row is persisted below.
        call = adapter.complete_json(
            schema=_DECISION_SCHEMA, system=prompt,
            user="screen this record", task_type=_TASK,
            prompt_version=PROMPT_VERSIONS[_TASK],
            protocol_revision=protocol.get("protocol_revision", 1),
            run_id=run_id)
        if not call["ok"]:
            unresolved.append({"uncertainty_id": unc["id"],
                               "paper_id": unc.get("paper_id"),
                               "reason": f"model call not usable "
                                          f"({call.get('outcome')})"})
            continue
        data = call["data"] or {}
        decision = str(data.get("decision") or "uncertain")
        if decision not in ("include", "exclude", "uncertain"):
            decision = "uncertain"
        criterion_ids = [c for c in (data.get("criterion_ids") or [])
                         if isinstance(c, str)]
        basis = str(data.get("evidence_basis") or "abstract")
        if basis not in ("abstract", "full_text", "metadata", "none"):
            basis = "abstract"
        rationale = str(data.get("rationale") or "")
        if decision == "uncertain":
            # Inconclusive: the queue row stays the single source of
            # truth (no duplicate row); it is marked unresolved after the
            # one bounded pass and reported, never force-decided
            # (spec section 9).
            store.update_uncertainty(unc["id"], status="unresolved",
                                     reason=rationale or "model recheck inconclusive",
                                     increment_rechecks=True)
            unresolved.append({"uncertainty_id": unc["id"],
                               "paper_id": unc.get("paper_id"),
                               "reason": rationale or "inconclusive"})
        else:
            store.record_screening(
                unc["paper_id"], stage="title_abstract", decision=decision,
                criterion_ids=criterion_ids, rationale=rationale,
                evidence_basis=basis,
                reviewer=f"model/{adapter.model}",
                prompt_version=PROMPT_VERSIONS[_TASK])
            if decision == "include":
                store.set_paper_access_level(unc["paper_id"], "abstract_only")
            store.update_uncertainty(unc["id"], status="resolved",
                                     reason=rationale,
                                     increment_rechecks=True)
            resolved += 1
            records.append({"uncertainty_id": unc["id"],
                            "paper_id": unc.get("paper_id"),
                            "decision": decision,
                            "criterion_ids": criterion_ids,
                            "evidence_basis": basis,
                            "prompt_version": PROMPT_VERSIONS[_TASK],
                            "reviewer": f"model/{adapter.model}",
                            "timestamp": utc_now_iso()})

    return {
        "task_type": _TASK,
        "run_id": run_id,
        "status": "succeeded",
        "prompt_version": PROMPT_VERSIONS[_TASK],
        "reviewer": f"model/{adapter.model}",
        "rechecked": len(queue) - len(unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
        "records": records,
        "note": "bounded single-pass model recheck; leftovers are reported "
                 "unresolved, never force-decided (spec section 9)",
    }
