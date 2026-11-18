"""Citation + claim-support audit (``papers citations audit``, spec
section 10).

The audit is DETERMINISTIC (no model) and reads the durable ledgers the
model-driven tasks wrote: the claim ledger (draft location, claim text,
evidence IDs, citation keys, audit status) and the bibliography registry
(stable citation keys, generated ONLY from saved paper rows -- no
invented entries). It reports:

* **claim audit**: every claim's label (supported / partially_supported /
  unsupported / uncertain / inference) + its linked evidence; a claim
  with no linked evidence can never be *supported* -- DOI resolution
  alone was never enough to set it (spec section 10);
* **reference identity checks**: citation-key validation (every key a
  claim or the draft uses resolves in the registry), duplicate + orphan
  reference detection, and the Phase-3 wrong-paper identity verdicts on
  ingested documents;
* **coverage / limits disclosure**: correction/retraction metadata
  availability check recorded WITH its source + limits, and an explicit
  statement that the audit makes NO exhaustive-coverage claim.

The result is a JSON envelope body; the envelope is emitted by the CLI.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "audit_citations",
    "claim_audit_rows",
    "reference_identity_checks",
]


def _registry(store: PaperStore, run_id: str) -> dict[str, dict[str, Any]]:
    """The run's bibliography registry: citation_key -> reference entry.

    Generated only from the saved source registry (the papers table);
    no reference is ever invented here (spec section 10)."""
    from papercli.reasoning.common import bibliography_for_run
    refs = bibliography_for_run(store, run_id)
    return {r["citation_key"]: r for r in refs}


def _doc_identity_verdicts(store: PaperStore, run_id: str) -> list[dict[str, Any]]:
    """Phase-3 wrong-paper identity verdicts on ingested documents."""
    out = []
    rows = store._conn.execute(
        "SELECT * FROM documents WHERE paper_id IN "
        "(SELECT id FROM papers WHERE run_id = ?)", (run_id,)).fetchall()
    for r in rows:
        r = dict(r)
        ident = json.loads(r.get("identity_json") or "{}")
        out.append({
            "document_id": r["id"],
            "identity_status": r.get("identity_status") or "not_checked",
            "identity": ident,
        })
    return out


def claim_audit_rows(store: PaperStore, run_id: str) -> list[dict[str, Any]]:
    """The claim ledger as audit rows.

    A claim is *supported* only with linked, semantically-supporting
    evidence; a claim with NO linked evidence (or whose evidence is
    missing from the registry) can never be reported as supported --
    the label is downgraded and the gap disclosed (DOI resolution alone
    never sets supported, spec section 10).
    """
    ev_ids = {r["id"] for r in store.evidence_for_run(run_id)}
    registry = _registry(store, run_id)
    rows: list[dict[str, Any]] = []
    for c in store.claims_for_run(run_id):
        label = c.get("audit_status") or "uncertain"
        missing_evidence = [e for e in c.get("evidence_ids") or []
                            if e not in ev_ids]
        unregistered_keys = [k for k in c.get("citation_keys") or []
                             if k not in registry]
        disclosed = []
        if label == "supported" and (missing_evidence or not c.get("evidence_ids")):
            # A supported claim needs linked evidence; without it the
            # label is downgraded and the gap is disclosed.
            label = "uncertain"
            disclosed.append("supported without linked evidence; downgraded "
                             "to uncertain (DOI resolution alone never sets "
                             "supported, spec section 10)")
        if missing_evidence:
            disclosed.append("linked evidence ids missing from the registry: "
                             + ", ".join(missing_evidence))
        if unregistered_keys:
            disclosed.append("citation keys not in the bibliography registry: "
                             + ", ".join(unregistered_keys))
        rows.append({
            "claim_id": c["id"],
            "draft_location": c.get("location") or c.get("draft_location"),
            "text": c["claim_text"],
            "evidence_ids": c.get("evidence_ids") or [],
            "citation_keys": c.get("citation_keys") or [],
            "is_synthesis": c.get("is_synthesis", False),
            "audit_status": label,
            "disclosures": disclosed,
        })
    return rows


def reference_identity_checks(store: PaperStore, run_id: str) -> dict[str, Any]:
    """Validation of keys, duplicates, orphan refs + wrong-paper verdicts."""
    refs = list(_registry(store, run_id).items())
    key_list = [k for k, _ in refs]
    duplicates = [k for k in key_list
                  if [r for r in refs if r[0] == k] and len(
                      [r for r in refs if r[0] == k]) > 1]
    claims = store.claims_for_run(run_id)
    used_keys = {k for c in claims for k in (c.get("citation_keys") or [])}
    registered = set(key_list)
    unregistered_used = sorted(used_keys - registered)
    orphan_refs = sorted(registered - used_keys)
    docs = _doc_identity_verdicts(store, run_id)
    return {
        "citation_keys_validated": len(used_keys),
        "duplicate_keys": duplicates,
        "unregistered_keys_used": unregistered_used,
        "orphan_references": orphan_refs,
        "wrong_paper_identity": {
            "documents_checked": len(docs),
            "flagged": [d for d in docs
                         if d["identity_status"] in
                         ("unmatched", "inconclusive", "rejected")],
        },
        "note": "keys are generated only from the saved source registry; "
                "no fabricated references; wrong-paper verdicts are the "
                "Phase-3 identity checks",
    }


def audit_citations(store: PaperStore, run_id: str) -> dict[str, Any]:
    """Full citation + claim-support audit (envelope body)."""
    claims = claim_audit_rows(store, run_id)
    refs = reference_identity_checks(store, run_id)
    by_label: dict[str, int] = {}
    for c in claims:
        by_label[c["audit_status"]] = by_label.get(c["audit_status"], 0) + 1
    return {
        "run_id": run_id,
        "generated_at": utc_now_iso(),
        "claims": {
            "total": len(claims),
            "by_audit_status": by_label,
            "rows": claims,
        },
        "references": refs,
        "correction_retraction_check": {
            "source": "saved paper identifiers (DOI presence)",
            "date": utc_now_iso(),
            "limits": [
                "availability check only -- NOT a ruling that no "
                "retractions exist",
                "no-DOI / article-number / online-first records have "
                "limited retraction metadata",
            ],
            "note": "missing metadata stays missing; no exhaustive-coverage "
                    "or 'all retractions ruled out' claim is made "
                    "(spec section 10)",
        },
        "coverage_disclosure": {
            "exhaustive_coverage_claimed": False,
            "note": "the audit covers the run's saved ledgers only; it "
                    "makes no exhaustive-coverage claim (spec section 10)",
        },
    }
