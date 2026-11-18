"""Theme-organized synthesis draft with registry-only citations.

Spec section 10: the draft is organized around the protocol's themes /
research question; it compares methods, evidence strength, results,
contradictions, limitations, and gaps, and includes negative /
contradictory evidence where found. Every citation key the model emits is
resolved against the run's bibliography registry (the model SELECTS
existing references and CANNOT fabricate entries -- unregistered keys are
dropped from the claim and flagged). Correction/retraction metadata
availability is checked and its source + limits are recorded (no
"all retractions ruled out" claim). The result is a claim ledger: each
substantive claim carries evidence IDs and an initial audit label.

No model configured -> ``skipped_no_model``.
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

__all__ = ["synthesize"]

_TASK = "synthesize"


def _evidence_digest(store: PaperStore, run_id: str,
                     cap: int = 400) -> list[dict[str, Any]]:
    """The evidence registry + claim ledger feeding the synthesis."""
    ev = store.evidence_for_run(run_id)
    slim = []
    for r in ev[:cap]:
        slim.append({
            "evidence_id": r["id"],
            "paper_id": r.get("paper_id"),
            "access_level": r.get("access_level"),
            "claim_type": r.get("claim_type"),
            "finding": r.get("finding"),
            "supporting_excerpt": (r.get("supporting_excerpt") or "")[:200],
            "population_or_system": r.get("population_or_system"),
            "limitations": r.get("limitations") or [],
            "extraction_warnings": r.get("extraction_warnings") or [],
        })
    return slim


def _synth_prompt(protocol: Mapping[str, Any], refs: list[dict[str, Any]],
                  evidence: list[dict[str, Any]],
                  corrections: Mapping[str, Any]) -> str:
    themes = protocol.get("themes_to_investigate") or [protocol.get("question")]
    key_block = "\n".join(
        f"- {r['citation_key']} ({r.get('title') or 'untitled'}, "
        f"{r.get('year') or 'n.d.'}, access={r.get('access_level') or 'unknown'})"
        for r in refs[:60])
    ev_block = json.dumps(evidence[:120], ensure_ascii=False, default=str)
    return (
        "You are a systematic-review synthesis writer. Compose a "
        "theme-organized draft. RULES: cite ONLY using the exact "
        "citation keys listed below -- never invent a key. Include "
        "negative/contradictory evidence and the study limitations. "
        "Do NOT claim exhaustive coverage; note what is NOT in scope. "
        "Cross-paper statements are synthesis (is_synthesis=true).\n"
        f"RESEARCH QUESTION: {protocol.get('question','')}\n"
        f"THEMES: {json.dumps(list(themes), ensure_ascii=False)}\n"
        f"CITATION KEYS AVAILABLE:\n{key_block}\n"
        f"RETRACTION/CORRECTION METADATA: "
        f"{json.dumps(corrections, ensure_ascii=False, default=str)}\n"
        f"EVIDENCE REGISTRY (evidence_id -> finding/excerpt/limits):\n{ev_block}\n"
        "Reply with a single JSON object: {\'themes\': [ {\'theme\', "
        "\'claims\': [ {\'text\', \'citation_keys\' [keys], "
        "\'evidence_ids\' [ids], \'is_synthesis\' bool } ], "
        "\'gaps\': [strings] } ], \'limitations\': [strings], "
        "\'contradictions\': [ {\'a\', \'b\', \'citation_keys\'} ]}"
    )


def _resolve_keys(claim: Mapping[str, Any],
                  registry: Mapping[str, Mapping[str, Any]]):
    """Every key must resolve in the registry; unregistered keys are
    dropped and flagged (spec section 10)."""
    kept, dropped = [], []
    for k in (claim.get("citation_keys") or []):
        k = str(k)
        if k in registry:
            kept.append(k)
        else:
            dropped.append(k)
    return kept, dropped


def _correction_check(store: PaperStore, run_id: str,
                      refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Check correction/retraction metadata availability and record its
    source + limits. Missing metadata STAYS missing; article numbers,
    online-first status, and no-DOI records are valid cases (spec 10)."""
    available, limits = 0, [
        "availability check only -- a check that ran on the saved "
        "identifiers, NOT a ruling that no retractions exist",
        "no-DOI / article-number / online-first records have limited "
        "retraction metadata and are recorded as such",
    ]
    checked: list[dict[str, Any]] = []
    for r in refs:
        ids = r.get("identifiers") or {}
        has_doi = bool(ids.get("doi"))
        status = "doi_present" if has_doi else "no_doi_or_article_number"
        if has_doi:
            available += 1
        checked.append({"citation_key": r["citation_key"],
                        "identifier_kind": status,
                        "retraction_metadata": None,
                        "note": "retraction/correction metadata not "
                                 "fetched in v1; recorded as available "
                                 "only when a DOI is present"})
    return {"source": "saved paper identifiers (DOI presence)",
            "date": utc_now_iso(),
            "references_with_doi": available,
            "references_total": len(refs),
            "limits": limits,
            "checked": checked,
            "note": "correction/retraction metadata availability check; "
                    "its source and limits are recorded, and no "
                    "exhaustive-coverage or 'all retractions ruled out' "
                    "claim is made (spec section 10)"}


def synthesize(store: PaperStore, task_payload: Mapping[str, Any],
               *, cfg: Any | None = None,
               adapter_factory=None) -> dict[str, Any]:
    """Build the theme-organized synthesis draft + claim ledger."""
    run_id = str(task_payload.get("run_id") or task_payload.get("id") or "")
    protocol = run_protocol(store, run_id)
    if cfg is None or not model_available(cfg):
        return skipped_result(_TASK, run_id)

    refs = bibliography_for_run(store, run_id)
    registry = {r["citation_key"]: r for r in refs}
    evidence = _evidence_digest(store, run_id)
    corrections = _correction_check(store, run_id, refs)

    adapter = build_adapter(cfg, limits=protocol.get("limits"),
                            store=store, run_id=run_id,
                            adapter_factory=adapter_factory)
    prompt = _synth_prompt(protocol, refs, evidence, corrections)
    schema = {
        "type": "object",
        "required": ["themes"],
        "properties": {
            "themes": {"type": "array",
                       "items": {"type": "object"}},
            "limitations": {"type": "array",
                            "items": {"type": "string"}},
            "contradictions": {"type": "array",
                               "items": {"type": "object"}},
        },
    }
    call = adapter.complete_json(
        schema=schema, system=prompt, user="synthesize the review",
        task_type=_TASK, prompt_version=PROMPT_VERSIONS[_TASK],
        protocol_revision=protocol.get("protocol_revision", 1),
        run_id=run_id, max_tokens=2048)

    draft: dict[str, Any] = {"themes": [], "limitations": [],
                             "contradictions": [], "claims": []}
    dropped_keys: list[dict[str, Any]] = []
    claim_count = 0
    if call["ok"]:
        data = call["data"] or {}
        for theme in data.get("themes") or []:
            if not isinstance(theme, dict):
                continue
            theme_out: dict[str, Any] = {"theme": theme.get("theme"),
                                         "claims": [], "gaps": theme.get("gaps") or []}
            for claim in theme.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                kept, dropped = _resolve_keys(claim, registry)
                for k in dropped:
                    dropped_keys.append({"theme": theme.get("theme"),
                                         "citation_key": k,
                                         "claim": claim.get("text"),
                                         "action": "dropped -- not in the "
                                                    "bibliography registry "
                                                    "(the model cannot "
                                                    "fabricate entries, "
                                                    "spec section 10)"})
                ev_ids = [e for e in (claim.get("evidence_ids") or [])
                          if isinstance(e, str)]
                # Initial audit label: a claim citing registered refs with
                # linked evidence is 'uncertain' until the critique task
                # verifies semantic support (DOI alone never sets
                # 'supported', spec section 10).
                audit = "uncertain"
                cid = store.record_claim(
                    run_id, claim_text=str(claim.get("text") or ""),
                    location=f"theme::{theme.get('theme')}",
                    evidence_ids=ev_ids, audit_status=audit,
                    is_synthesis=bool(claim.get("is_synthesis")),
                    citation_keys=kept)
                claim_count += 1
                claim_row = {"claim_id": cid,
                             "text": claim.get("text"),
                             "citation_keys": kept,
                             "evidence_ids": ev_ids,
                             "is_synthesis": bool(claim.get("is_synthesis")),
                             "initial_audit": audit}
                theme_out["claims"].append(claim_row)
                draft["claims"].append(claim_row)
            draft["themes"].append(theme_out)
        draft["limitations"] = [s for s in (data.get("limitations") or [])
                                 if isinstance(s, str)]
        draft["contradictions"] = [c for c in (data.get("contradictions") or [])
                                    if isinstance(c, dict)]

    artifact_id = store.record_artifact(
        run_id, type_="synthesis_draft",
        path=f"runs/{run_id}/synthesis_draft.json",
        content_hash=None, run_revision=str(protocol.get("protocol_revision")),
        generation_status="complete" if call["ok"] else "partial")
    draft["correction_check"] = corrections
    draft["dropped_citation_keys"] = dropped_keys
    draft["artifact_id"] = artifact_id
    draft["model_call_outcome"] = call.get("outcome")
    draft["note"] = ("theme-organized; registry-only citations; "
                      "negative/contradictory evidence + limitations + "
                      "gaps included; correction/retraction check recorded "
                      "with its limits; no exhaustive-coverage claim "
                      "(spec section 10)")
    return {
        "task_type": _TASK,
        "run_id": run_id,
        "status": "succeeded" if call["ok"] else "partial",
        "prompt_version": PROMPT_VERSIONS[_TASK],
        "reviewer": f"model/{adapter.model}",
        "claims_recorded": claim_count,
        "dropped_citation_keys": dropped_keys,
        "correction_check": corrections,
        "draft": draft,
        "artifact_id": artifact_id,
    }
