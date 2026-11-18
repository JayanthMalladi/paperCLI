"""Bounded search expansion (Phase 4, spec sections 8 + 19).

From accepted records, add citation-related candidates (OpenAlex forward
citation discovery via the ``cites`` filter -- the works that CITE the
seed; see :data:`OPENALEX_EXPANSION_METHOD` for the live-verified
direction; Semantic Scholar relations when available, with keyless
graceful-degrade) up to the protocol's ``max_discovered_records``. Every
expansion records its provenance chain (seed -> via provider X
query/cursor/date) and a provider failure marks that branch **incomplete**,
never "complete" (spec section 19: a provider failure is never a complete
search).

The provider call layer is injectable (``adapter_factory``) so the tests
drive it with fixture adapters -- no network.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "expand_search",
    "candidate_records_from_openalex",
    "candidate_records_from_s2",
    "PROVENANCE_KIND",
]

PROVENANCE_KIND = "expansion"

#: The OpenAlex expansion direction: FORWARD discovery -- the works that
#: CITE the seed (expand-to-citing-works). Live-verified keyless on
#: 2026-11-18: for a seed with ``cited_by_count``=N, ``cites:<seed>``
#: (and ``referenced_works:<seed>``) returns N works -- external citing
#: works, sample publication years >= the seed's, none present in the
#: seed's own reference list. Despite its name, ``cited_by:<seed>`` is
#: the BACKWARD direction: it returns the works the seed itself cites
#: (count ~ the seed's ``referenced_works_count``; all sample items
#: present in the seed's own reference list, years < the seed's). The
#: previously shipped ``citations:`` filter does not exist in OpenAlex
#: and 400s live.
OPENALEX_EXPANSION_METHOD = "cites"

#: OpenAlex citation-relationship filter fields that actually exist in
#: the live API (the filter names expansion is allowed to use; OpenAlex
#: 400s on any other field, e.g. the non-existent ``citations``).
#: Directions (live-verified 2026-11-18, keyless free tier):
#: * ``cites:<seed>`` / ``referenced_works:<seed>`` -- FORWARD: the works
#:   that cite the seed (count ~ the seed's ``cited_by_count``);
#: * ``cited_by:<seed>`` -- BACKWARD: the works the seed cites (count ~
#:   the seed's ``referenced_works_count``) -- counter-intuitive naming;
#: * ``referenced_works_count`` / ``has_references`` -- scalar/boolean
#:   reference fields, not directional.
OPENALEX_VALID_CITATION_FILTERS = frozenset({
    "cited_by", "cites", "referenced_works",
    "referenced_works_count", "has_references",
})


# ---------------------------------------------------------------------------
# Candidate record mappers (Phase-2 shared record shape)
# ---------------------------------------------------------------------------

def candidate_records_from_openalex(works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map OpenAlex ``/works`` items to the shared record shape."""
    out: list[dict[str, Any]] = []
    for w in works or []:
        authors = [a.get("author", {}).get("display_name")
                   for a in (w.get("authorships") or [])
                   if a.get("author", {}).get("display_name")]
        date = (w.get("publication_date") or "").strip()[:4]
        oa = w.get("open_access") or {}
        primary = w.get("primary_location") or {}
        doi = ((w.get("doi") or "").replace("https://doi.org/", "")).lower() or None
        out.append({
            "provider": "openalex",
            "openalex_id": w.get("id"),
            "doi": doi,
            "title": w.get("display_name") or w.get("title"),
            "authors": authors or None,
            "year": int(date) if date.isdigit() else None,
            "url": oa.get("oa_url") or primary.get("landing_page_url")
            or (f"https://doi.org/{doi}" if doi else None),
        })
    return out


def candidate_records_from_s2(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map S2 ``citations``/``references`` items to the shared shape."""
    out: list[dict[str, Any]] = []
    for p in papers or []:
        ext = p.get("externalIds") or {}
        ystr = str(p.get("year") or p.get("publicationYear") or "")
        out.append({
            "provider": "s2",
            "s2_id": p.get("paperId"),
            "doi": ext.get("DOI"),
            "arxiv_id": ext.get("ArXiv"),
            "title": p.get("title"),
            "authors": [a.get("name") for a in (p.get("authors") or [])
                        if a.get("name")],
            "year": int(ystr) if ystr.isdigit() else None,
        })
    return out


def _record_ids(rec: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    if rec.get("doi"):
        ids["doi"] = str(rec["doi"]).lower()
    if rec.get("arxiv_id"):
        ids["arxiv_id"] = str(rec["arxiv_id"])
    if rec.get("s2_id"):
        ids["s2"] = str(rec["s2_id"])
    if rec.get("openalex_id"):
        ids["openalex"] = str(rec["openalex_id"])
    return ids


# ---------------------------------------------------------------------------
# Branch execution
# ---------------------------------------------------------------------------

def _openalex_branch(adapter, seed_ids: Mapping[str, str], config: Any,
                     env: Mapping[str, str], cache, per_page: int,
                     method: str = OPENALEX_EXPANSION_METHOD) -> dict[str, Any]:
    """One OpenAlex citation-relationship query.

    Forward direction by default: ``cites:<work>`` returns the works
    that CITE the seed (expand-to-citing-works; live-verified -- see
    :data:`OPENALEX_EXPANSION_METHOD`). ``method`` is the filter field
    name and must be one of
    :data:`OPENALEX_VALID_CITATION_FILTERS` (the OpenAlex API 400s on
    any other field name, e.g. the non-existent ``citations``).
    """
    if method not in OPENALEX_VALID_CITATION_FILTERS:
        raise ValueError(
            f"OpenAlex filter {method!r} is not a valid citation-relationship "
            f"field; use one of {sorted(OPENALEX_VALID_CITATION_FILTERS)}")
    work = seed_ids.get("openalex")
    filt = f"{method}:{work}"
    result = adapter.search_works(
        "", config, env, cache=cache, per_page=per_page,
        filters=[filt], use_search=False,
    )
    if not isinstance(result, dict):
        result = {"error": "malformed provider result"}
    records = candidate_records_from_openalex(result.get("items") or [])
    completeness = result.get("completeness", "incomplete")
    if result.get("error"):
        completeness = "incomplete"
    return {
        "provider": "openalex",
        "method": method,
        "query": filt,
        "cursor": result.get("next_cursor"),
        "result_count": len(records),
        "total_results": result.get("total_results"),
        "completeness": completeness,
        "error": result.get("error"),
        "records": records,
        "executed_at": result.get("fetched_at") or utc_now_iso(),
        "seed_work": work,
    }


def _s2_branch(adapter, seed_ids: Mapping[str, str], config: Any,
               env: Mapping[str, str], cache, limit: int) -> dict[str, Any]:
    """One S2 relations call (citations + references in one envelope).

    Keyless access degrades gracefully: a recorded skip is a data point,
    never a branch error and never a run failure (Phase-2 rule).
    """
    s2id = seed_ids.get("s2")
    result = adapter.related_papers(s2id, config, env, cache=cache, limit=limit)
    if not isinstance(result, dict):
        result = {"skipped": True, "reason": "malformed provider result",
                  "data": None}
    if result.get("skipped"):
        return {
            "provider": "s2", "method": "relations", "query": s2id,
            "cursor": None, "result_count": 0, "total_results": None,
            "completeness": "skipped", "error": None,
            "skip_reason": result.get("reason"), "records": [],
            "executed_at": utc_now_iso(), "seed_s2": s2id,
        }
    data = result.get("data") or {}
    recs = candidate_records_from_s2(
        (data.get("citations") or []) + (data.get("references") or []))
    return {
        "provider": "s2", "method": "relations", "query": s2id,
        "cursor": None, "result_count": len(recs), "total_results": None,
        "completeness": "incomplete", "error": None, "records": recs,
        "executed_at": result.get("fetched_at") or utc_now_iso(),
        "seed_s2": s2id,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def expand_search(
    store: PaperStore,
    run_id: str,
    protocol: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    *,
    config: Any,
    env: Mapping[str, str] | None = None,
    cache: Any = None,
    adapter_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run the bounded expansion for the accepted records of a run.

    ``accepted``: paper rows or record dicts whose title/abstract screening
    decision is ``include`` (or full-text include). Each seed that carries
    an OpenAlex work id gets an OpenAlex citation branch; each seed that
    carries an S2 id gets an S2 relations branch. Candidate records are
    upserted into the run's paper registry (provenance preserved) and each
    branch is persisted as a Search row via :meth:`PaperStore.record_expansion`.

    Caps: the protocol's ``max_discovered_records`` bounds total records
    in the run; when the cap is hit, remaining candidates are recorded in
    the report as ``deferred`` (never silently dropped).

    Returns the expansion report::

        {"seeds", "branches": [ {provider, method, seed_paper_id, query,
          cursor, executed_at, result_count, completeness, error,
          skip_reason, candidates_added, deferred} ],
         "added", "deferred", "cap", "complete", "incomplete_branches"}
    """
    limits = protocol.get("limits") or {}
    cap = int(limits.get("max_discovered_records") or 0)
    per_page = max(1, min(int(limits.get("records_per_query_estimate") or 50), 25))

    if adapter_factory is None:
        def adapter_factory(name: str):  # noqa: F811 -- registry default
            from papercli.providers.registry import get_registry
            import papercli.providers.openalex  # noqa: F401 (register)
            import papercli.providers.semantic_scholar  # noqa: F401
            return get_registry().get(name)

    oa = adapter_factory("openalex")
    s2 = adapter_factory("s2")

    existing = store.papers_for_run(run_id)
    known_ids: set[tuple[str, str]] = set()
    for row in existing:
        ids = json.loads(row.get("canonical_ids_json") or "{}")
        for k, v in ids.items():
            known_ids.add((k, str(v)))

    branches: list[dict[str, Any]] = []
    added_total = 0
    deferred_total = 0
    incomplete: list[str] = []

    for seed in accepted:
        if "canonical_ids_json" in seed:
            seed_ids = json.loads(seed.get("canonical_ids_json") or "{}")
        elif "canonical_ids" in seed:
            seed_ids = dict(seed.get("canonical_ids") or {})
        else:
            seed_ids = _record_ids(seed)
        seed_paper_id = (seed.get("paper_id")
                         if isinstance(seed, Mapping) and seed.get("paper_id")
                         else (seed.get("id") if isinstance(seed, Mapping)
                               and seed.get("run_id") else None))

        # OpenAlex branch: FORWARD citation discovery (cites = the works
        # that cite the seed; live-verified filter field -- the
        # counter-intuitively-named ``cited_by`` filter is the backward
        # direction, see OPENALEX_EXPANSION_METHOD).
        if seed_ids.get("openalex"):
            try:
                b = _openalex_branch(oa, seed_ids, config, env, cache,
                                     per_page,
                                     OPENALEX_EXPANSION_METHOD)
            except Exception as e:  # noqa: BLE001 -- branch failure recorded
                b = {"provider": "openalex",
                     "method": OPENALEX_EXPANSION_METHOD,
                     "query": f"{OPENALEX_EXPANSION_METHOD}:"
                              f"{seed_ids.get('openalex')}",
                     "cursor": None, "result_count": 0, "total_results": None,
                     "completeness": "incomplete",
                     "error": f"{type(e).__name__}: {e}", "records": [],
                     "executed_at": utc_now_iso()}
            branches.append(_finalize_branch(
                store, run_id, seed_paper_id, b, cap, known_ids,
                added_total, seed_ids))
        # S2 branch (graceful-degrade; keyless skip is recorded, not fatal).
        if seed_ids.get("s2"):
            try:
                b = _s2_branch(s2, seed_ids, config, env, cache,
                               max(5, per_page // 2))
            except Exception as e:  # noqa: BLE001
                b = {"provider": "s2", "method": "relations",
                     "query": seed_ids.get("s2"), "cursor": None,
                     "result_count": 0, "total_results": None,
                     "completeness": "incomplete",
                     "error": f"{type(e).__name__}: {e}", "records": [],
                     "executed_at": utc_now_iso()}
            branches.append(_finalize_branch(
                store, run_id, seed_paper_id, b, cap, known_ids,
                added_total, seed_ids))

    # Recompute the running totals from the persisted branches.
    for b in branches:
        added_total += b.get("candidates_added") or 0
        deferred_total += b.get("deferred") or 0
        # Spec section 19: an expansion is "complete" only when EVERY branch
        # was fully exhausted. A provider failure (incomplete), a partial
        # page (partial), or a keyless skip (skipped) each make the run's
        # expansion not complete -- a failure is never a complete search.
        if b.get("completeness") != "complete":
            incomplete.append(f"{b['provider']}/{b['method']}:"
                              f"{b.get('query')}")

    return {
        "seeds": len(list(accepted)),
        "branches": branches,
        "added": added_total,
        "deferred": deferred_total,
        "cap": cap,
        "complete": not incomplete,
        "incomplete_branches": incomplete,
        "note": ("A branch error/skip is never a complete search "
                 "(spec section 19); skipped branches are recorded, not "
                 "counted as coverage."),
    }


def _finalize_branch(
    store: PaperStore, run_id: str, seed_paper_id: str | None,
    branch: dict[str, Any], cap: int, known_ids: set[tuple[str, str]],
    running_added: int, seed_ids: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one branch: upsert new papers, record the Search row."""
    provenance = _record_provenance(seed_paper_id, branch, seed_ids)
    candidates_added = 0
    deferred = 0
    for rec in branch.get("records") or []:
        rid = _record_ids(rec)
        key_sig = frozenset(rid.items())
        already = any((k, v) in known_ids for k, v in key_sig)
        if cap and running_added + candidates_added >= cap:
            deferred += 1
            continue
        if already:
            # Provenance link only: the paper already exists in the run.
            continue
        store.upsert_paper(
            run_id, rid, title=rec.get("title"), authors=rec.get("authors"),
            year=rec.get("year"),
            metadata_provenance={"kind": PROVENANCE_KIND,
                                 "provenance": provenance},
        )
        for k, v in rid.items():
            known_ids.add((k, str(v)))
        candidates_added += 1
    running_added += candidates_added
    branch["candidates_added"] = candidates_added
    branch["deferred"] = deferred
    branch["seed_paper_id"] = seed_paper_id
    branch["provenance_chain"] = provenance["chain"]
    store.record_expansion(
        run_id, seed_paper_id or "", provider=branch.get("provider", "?"),
        method=branch.get("method", "?"), query=branch.get("query") or "",
        result={
            "cursor": branch.get("cursor"),
            "result_count": branch.get("result_count"),
            "completeness": branch.get("completeness"),
            "error": branch.get("error"),
            "skip_reason": branch.get("skip_reason"),
            "provenance": provenance,
            "candidates_added": candidates_added,
            "deferred": deferred,
        },
    )
    return branch


def _record_provenance(seed_paper_id: str | None, branch: dict[str, Any],
                       seed_ids: Mapping[str, Any]) -> dict[str, Any]:
    """The seed -> via provider X query/cursor/date chain (spec section 19)."""
    chain = (
        f"seed:{seed_paper_id or seed_ids.get('openalex') or seed_ids.get('s2')}"
        f"->via:{branch.get('provider')}:"
        f"{branch.get('method')}:"
        f"query={branch.get('query')}:"
        f"cursor={branch.get('cursor') or 'none'}:"
        f"date={branch.get('executed_at')}")
    return {
        "kind": PROVENANCE_KIND,
        "seed_paper_id": seed_paper_id,
        "seed_ids": dict(seed_ids),
        "provider": branch.get("provider"),
        "method": branch.get("method"),
        "query": branch.get("query"),
        "cursor": branch.get("cursor"),
        "executed_at": branch.get("executed_at"),
        "chain": chain,
    }


