"""Claim audit + bounded revision (spec section 10).

Every substantive claim in the draft is audited against its linked
evidence and assigned one of the published labels:
``supported`` / ``partially_supported`` / ``unsupported`` /
``uncertain`` / ``inference``. Rules:

* **DOI resolution alone never sets a claim to ``supported``** -- a
  claim needs linked evidence that SEMANTICALLY supports it;
* a claim with no resolvable evidence link is at most ``uncertain``;
* cross-paper statements / author interpretations are labeled
  ``inference`` (or ``partially_supported`` when grounded) and flagged
  ``is_synthesis``;
* **unsupported** claims are removed, narrowed, or prominently marked
  unresolved in the bounded revision pass (exactly one);
* the automated critique + revision records are preserved.

No model configured -> ``skipped_no_model``; the draft's claims stay at
their initial ``uncertain`` labels (deterministic, never a fabricated
"supported").
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.reasoning.common import (
    PROMPT_VERSIONS,
    bibliography_for_run,
    build_adapter,
    model_available,
    run_protocol,
    skipped_result,
)
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = ["critique", "AUDIT_LABELS"]

_TASK = "critique"

#: Published audit labels (spec section 10 / claims schema enum).
AUDIT_LABELS = ("supported", "partially_supported", "unsupported",
                "uncertain", "inference")


def _claim_blocks(claims: list[Mapping[str, Any]],
                  evidence_by_id: Mapping[str, Mapping[str, Any]],
                  registry: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-claim audit input: the claim text, its evidence, and citation
    metadata (for the semantic-support judgement)."""
    out = []
    for c in claims:
        evs = []
        for eid in c.get("evidence_ids") or []:
            e = evidence_by_id.get(eid)
            if e is None:
                evs.append({"evidence_id": eid, "missing": True})
            else:
                evs.append({
                    "evidence_id": eid,
                    "access_level": e.get("access_level"),
                    "claim_type": e.get("claim_type"),
                    "finding": e.get("finding"),
                    "supporting_excerpt": (e.get("supporting_excerpt") or "")[:200],
                    "population_or_system": e.get("population_or_system"),
                })
        out.append({
            "claim_id": c.get("claim_id") or c.get("claim_text"),
            "text": c.get("text"),
            "citation_keys": c.get("citation_keys") or [],
            "evidence": evs,
            "is_synthesis": c.get("is_synthesis", False),
        })
    return out


def _critique_prompt(question: str, blocks: list[dict[str, Any]]) -> str:
    return (
        "You are a claim-support auditor. For EACH claim, label it using "
        "exactly one of: supported, partially_supported, unsupported, "
        "uncertain, inference. RULES: a claim is 'supported' only when a "
        "linked evidence record SEMANTICALLY supports it -- resolving a "
        "DOI alone is never enough. No resolvable evidence -> at most "
        "'uncertain'. A cross-paper / author-interpretation statement -> "
        "'inference' (or 'partially_supported' if grounded in evidence) "
        "and set is_synthesis=true. Reply with a single JSON object: "
        "{\'claims\': [{\"claim_id\", \'label\', \'reason\', "
        "\'action\': \'keep\'|\'narrow\'|\'mark_unresolved\', "
        "\'narrowed_text\': string, \'is_synthesis\': bool}]}\n"
        f"RESEARCH QUESTION: {question}\n"
        f"CLAIMS:\n{json.dumps(blocks, ensure_ascii=False, default=str)}"
    )


def critique(store: PaperStore, task_payload: Mapping[str, Any],
             *, cfg: Any | None = None,
             adapter_factory=None) -> dict[str, Any]:
    """Audit every draft claim + one bounded revision pass."""
    run_id = str(task_payload.get("run_id") or task_payload.get("id") or "")
    protocol = run_protocol(store, run_id)
    claims = store.claims_for_run(run_id)
    if not claims:
        return {"task_type": _TASK, "run_id": run_id, "status": "succeeded",
                "claims_audited": 0, "note": "no claims to audit yet"}
    if cfg is None or not model_available(cfg):
        return skipped_result(_TASK, run_id, claims_total=len(claims))

    evidence_by_id = {r["id"]: r for r in store.evidence_for_run(run_id)}
    registry = {r["citation_key"]: r for r in bibliography_for_run(store, run_id)}

    adapter = build_adapter(cfg, limits=protocol.get("limits"),
                            store=store, run_id=run_id,
                            adapter_factory=adapter_factory)
    blocks = _claim_blocks(claims, evidence_by_id, registry)
    schema = {
        "type": "object",
        "required": ["claims"],
        "properties": {"claims": {"type": "array",
                                   "items": {"type": "object"}}},
    }
    call = adapter.complete_json(
        schema=schema, system=_critique_prompt(str(protocol.get("question", "")), blocks),
        user="audit these claims", task_type=_TASK,
        prompt_version=PROMPT_VERSIONS[_TASK],
        protocol_revision=protocol.get("protocol_revision", 1),
        run_id=run_id, max_tokens=2048)

    audits: list[dict[str, Any]] = []
    label_by_id: dict[str, str] = {}
    if call["ok"]:
        for c in (call["data"] or {}).get("claims") or []:
            label = str(c.get("label") or "uncertain")
            if label not in AUDIT_LABELS:
                label = "uncertain"
            audits.append({"claim_id": c.get("claim_id"),
                           "label": label, "reason": c.get("reason"),
                           "action": c.get("action") or "keep",
                           "narrowed_text": c.get("narrowed_text"),
                           "is_synthesis": bool(c.get("is_synthesis", False))})
            label_by_id[str(c.get("claim_id"))] = label
    else:
        # No model usable: keep the initial uncertain labels, disclose it.
        audits = [{"claim_id": c.get("claim_id"), "label": "uncertain",
                   "reason": "model critique unavailable", "action": "keep"}
                  for c in claims]

    # Bounded revision: apply the single pass -- unsupported claims are
    # removed / narrowed / prominently marked unresolved.
    unsupported, narrowed, kept = [], [], []
    for c in claims:
        cid = str(c.get("id") or c.get("claim_id") or "")
        audit = next((a for a in audits if str(a.get("claim_id")) == cid),
                     {"label": "uncertain", "action": "keep",
                      "narrowed_text": None})
        label = audit["label"]
        action = audit.get("action") or "keep"
        if label == "unsupported":
            if action in ("narrow",) and audit.get("narrowed_text"):
                narrowed.append({"claim_id": cid, "text": audit["narrowed_text"],
                                 "label": "partially_supported"})
                final_label = "partially_supported"
            else:
                kept.append({"claim_id": cid, "text": c.get("claim_text"),
                             "label": "unsupported",
                             "marked_unresolved": True})
                final_label = "unsupported"
        else:
            kept.append({"claim_id": cid, "text": c.get("claim_text"),
                         "label": label})
            final_label = label
        if cid:
            with store._conn:
                store._conn.execute(
                    "UPDATE claims SET audit_status = ? WHERE id = ?",
                    (final_label, cid))

    return {
        "task_type": _TASK,
        "run_id": run_id,
        "status": "succeeded",
        "prompt_version": PROMPT_VERSIONS[_TASK],
        "reviewer": f"model/{adapter.model}",
        "claims_audited": len(claims),
        "labels_applied": label_by_id,
        "audits": audits,
        "revision": {
            "pass": 1,
            "max_passes": 1,
            "kept": kept,
            "narrowed": narrowed,
            "unsupported_marked_unresolved": [
                k for k in kept if k.get("label") == "unsupported"],
        },
        "model_call_outcome": call.get("outcome"),
        "note": ("DOI resolution alone never sets 'supported'; a claim "
                 "needs linked evidence that semantically supports it. "
                 "Unsupported claims are removed / narrowed / prominently "
                 "marked unresolved in exactly one bounded revision pass; "
                 "automated critique + revision records are preserved "
                 "(spec section 10)"),
    }
