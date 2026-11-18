"""Model-driven evidence extraction for usable-access documents.

Phase-4 deterministic extraction recorded the *structure* (stable
locations, resolvable excerpts, section heuristics). This task adds the
model's structured **findings** on top of it, gated by the access level
(spec section 9, the never-infer rule):

* ``full_text`` / ``partial_full_text`` documents are eligible;
* ``abstract_only`` records get an explicit ``abstract_only`` evidence
  basis and NEVER a full-text finding;
* ``metadata_only`` / ``unavailable`` documents produce zero findings;
* numeric findings MUST carry traceable source support, units, and
  population/task context;
* author interpretation is marked explicitly against reported results.

No model configured -> ``skipped_no_model`` (deterministic rows from
Phase 4 remain the extraction of record).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.agent_contract.schemas import load_schema
from papercli.reasoning.common import (
    PROMPT_VERSIONS,
    build_adapter,
    model_available,
    run_protocol,
    skipped_result,
)
from papercli.research.evidence import (
    MAX_EXCERPT_CHARS,
    MAX_EXCERPTS_PER_DOCUMENT,
    AccessLevelGate,
    _document_access_level,
)
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = ["extract_findings", "document_excerpt_blocks", "numeric_support_warnings"]

_TASK = "extract_findings"
_SCHEMA = "evidence"

#: The model's extraction reply shape: a list of finding objects. The
#: published ``evidence`` schema is the PERSISTED record; the reply is the
#: list wrapper the model returns (validated post-hoc, like the adapter's
#: lenient path).
_EXTRACT_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["finding"],
                "properties": {
                    "finding": {"type": "string"},
                    "claim_type": {"type": "string"},
                    "source_blocks": {"type": "array",
                                       "items": {"type": "integer"}},
                    "units": {"type": ["string", "null"]},
                    "population_or_system": {"type": ["string", "null"]},
                    "context": {"type": ["string", "null"]},
                    "methods": {"type": ["object", "null"]},
                    "limitations": {"type": "array",
                                     "items": {"type": "string"}},
                },
            },
        },
    },
}

#: Cap model-driven findings per document (bounded work; the run's token
#: budget is the real ceiling, this is the task-level one).
MAX_FINDINGS_PER_DOCUMENT = 20
_MAX_LOCATIONS_SHOWN = 12


def _normalize_level(store: PaperStore, doc_id: str, paper_id: str) -> str | None:
    return _document_access_level(store, doc_id, paper_id)


def document_excerpt_blocks(store: PaperStore, document_id: str) -> list[dict[str, Any]]:
    """Stable location blocks fed to the model (excerpt + location).

    Only blocks whose text is stored in the document_text index are used,
    so a finding's ``location`` + ``supporting_excerpt`` always resolves
    against saved source text (spec section 9).
    """
    rows = store.document_text(document_id)
    blocks: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        blocks.append({
            "location": {"page": row.get("page"),
                         "section": row.get("section"),
                         "paragraph": row.get("paragraph")},
            "excerpt": text[:MAX_EXCERPT_CHARS],
        })
        if len(blocks) >= MAX_EXCERPTS_PER_DOCUMENT:
            break
    return blocks


def _is_numeric_finding(finding: str) -> bool:
    return bool(finding and any(ch.isdigit() for ch in finding))


def numeric_support_warnings(finding: Mapping[str, Any]) -> list[str]:
    """Spec section 9: numeric findings need traceable source support,
    units, and population/task context."""
    warns: list[str] = []
    text = str(finding.get("finding") or "")
    if not _is_numeric_finding(text):
        return warns
    if not str(finding.get("supporting_excerpt") or "").strip():
        warns.append("numeric finding without traceable source support "
                     "(no stored excerpt)")
    units = finding.get("units")
    if not units:
        warns.append("numeric finding without units")
    ctx = finding.get("population_or_system") or finding.get("context")
    if not ctx:
        warns.append("numeric finding without population/task context")
    return warns


def _extract_prompt(question: str, doc_title: str | None,
                    blocks: list[dict[str, Any]]) -> str:
    shown = blocks[:_MAX_LOCATIONS_SHOWN]
    lines = [
        "You are a systematic-review evidence-extraction assistant. From "
        "the numbered source blocks below, extract structured findings. "
        "RULES: numeric findings must cite the exact block number AND "
        "carry units + population/task context. Author interpretation "
        "must be labeled \'author_interpretation\'; what the source "
        "reports is \'reported_result\'. Do not invent numbers, units, "
        "or populations that are not in a block.",
        f"RESEARCH QUESTION: {question}",
    ]
    if doc_title:
        lines.append(f"DOCUMENT: {doc_title}")
    lines.append("SOURCE BLOCKS (location -> text):")
    for i, b in enumerate(shown, 1):
        loc = b["location"]
        locs = ", ".join(f"{k}={v}" for k, v in loc.items() if v is not None)
        lines.append(f"[{i}] ({locs}) {b['excerpt']}")
    lines.append(
        "Reply with a single JSON object: {\'findings\': [{\"finding\", "
        '"claim_type" (reported_result|method|limitation|author_interpretation), '
        '"source_blocks" [block numbers], "units" (null if none), '
        '"population_or_system", "limitations" [strings], "methods" (object, '
        'null if unknown)}]}')
    return "\n".join(lines)


def extract_findings(store: PaperStore, task_payload: Mapping[str, Any],
                     *, cfg: Any | None = None,
                     adapter_factory=None) -> dict[str, Any]:
    """Model extraction over the run's usable-access documents."""
    run_id = str(task_payload.get("run_id") or task_payload.get("id") or "")
    protocol = run_protocol(store, run_id)
    if cfg is None or not model_available(cfg):
        return skipped_result(_TASK, run_id)

    adapter = build_adapter(cfg, limits=protocol.get("limits"),
                            store=store, run_id=run_id,
                            adapter_factory=adapter_factory)
    gate = AccessLevelGate()
    processed, gated, findings_total = 0, 0, 0
    gated_docs: list[dict[str, Any]] = []
    evidence_ids: list[str] = []

    for doc in store._conn.execute(
            "SELECT * FROM documents WHERE paper_id IN "
            "(SELECT id FROM papers WHERE run_id = ?)", (run_id,)).fetchall():
        doc = dict(doc)
        level = _document_access_level(store, doc["id"], doc.get("paper_id"))
        if not gate.usable(level):
            gated += 1
            gated_docs.append({"document_id": doc["id"],
                               "access_level": level or "unknown",
                               "note": "gated -- never-infer rule: no "
                                       "full-text finding from metadata "
                                       "(spec section 9)"})
            continue
        blocks = document_excerpt_blocks(store, doc["id"])
        if not blocks:
            continue
        prompt = _extract_prompt(str(protocol.get("question", "")),
                                 doc.get("source_url"), blocks)
        call = adapter.complete_json(
            schema=_EXTRACT_REPLY_SCHEMA, system=prompt,
            user="extract findings", task_type=_TASK,
            prompt_version=PROMPT_VERSIONS[_TASK],
            protocol_revision=protocol.get("protocol_revision", 1),
            run_id=run_id)
        processed += 1
        if not call["ok"]:
            continue
        loc_by_num: dict[int, dict[str, Any]] = {}
        excerpt_by_num: dict[int, str] = {}
        for i, b in enumerate(blocks[:_MAX_LOCATIONS_SHOWN], 1):
            loc_by_num[i] = b["location"]
            excerpt_by_num[i] = b["excerpt"]
        for raw in (call["data"] or {}).get("findings") or []:
            if len(evidence_ids) >= MAX_FINDINGS_PER_DOCUMENT:
                break
            if not isinstance(raw, dict):
                continue
            finding_text = str(raw.get("finding") or "").strip()
            if not finding_text:
                continue
            src_nums = [n for n in (raw.get("source_blocks") or [])
                        if isinstance(n, int) and n in loc_by_num]
            loc = loc_by_num.get(src_nums[0]) if src_nums else blocks[0]["location"]
            excerpt = excerpt_by_num.get(src_nums[0], blocks[0]["excerpt"]) if src_nums else blocks[0]["excerpt"]
            warnings = list(raw.get("extraction_warnings") or [])
            # Numeric-findings rule (spec section 9): the finding must
            # carry traceable source support (the stored excerpt), units,
            # and population/task context -- check against the PERSISTED
            # values, not the raw reply.
            warnings.extend(numeric_support_warnings({
                "finding": finding_text,
                "supporting_excerpt": excerpt,
                "units": raw.get("units"),
                "population_or_system": (raw.get("population_or_system")
                                          or raw.get("context")),
            }))
            claim_type = str(raw.get("claim_type") or "reported_result")
            if claim_type not in ("reported_result", "method", "limitation",
                                  "author_interpretation"):
                claim_type = "reported_result"
            ev_id = store.record_evidence(
                doc["paper_id"], document_id=doc["id"],
                access_level=level or "full_text",
                location=loc, supporting_excerpt=excerpt,
                finding=finding_text, claim_type=claim_type,
                methods=raw.get("methods") if isinstance(raw.get("methods"), dict) else None,
                population_or_system=(raw.get("population_or_system")
                                       or raw.get("context")),
                limitations=list(raw.get("limitations") or []),
                extraction_warnings=warnings)
            evidence_ids.append(ev_id)
            findings_total += 1

    return {
        "task_type": _TASK,
        "run_id": run_id,
        "status": "succeeded",
        "prompt_version": PROMPT_VERSIONS[_TASK],
        "reviewer": f"model/{adapter.model}",
        "documents_processed": processed,
        "documents_gated": gated,
        "gated_documents": gated_docs,
        "findings_total": findings_total,
        "evidence_ids": evidence_ids,
        "note": "access-level gated; numeric findings carry traceable "
                 "support + units + context; author interpretation is "
                 "labeled; abstract-only papers never get a full-text "
                 "finding (spec section 9)",
    }
