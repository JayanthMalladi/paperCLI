"""Research protocol planning (Phase 4, spec sections 5 + 7).

``papers review plan --brief <file> --out review-plan.yaml``:

1. Load the brief (YAML; the ``.md`` spelling is accepted -- it is still
   YAML content) and validate it against the Phase-1 brief schema
   (required fields: question, purpose, scope, desired outputs, resource
   limits). Missing essential scope/configuration is a preflight failure
   (exit 2, the missing field named), never a wait.
2. Apply documented defaults to the optional fields and RECORD them in
   the saved protocol (spec section 7: "defaults may fill optional fields
   but must appear in the saved protocol").
3. Expand a deterministic, database-specific query plan per provider
   (query text, filters, date range, include_preprints) -- NO model
   calls: the standalone model-driven refinement is Phase 5.
4. Produce a resource estimate (expected query count, expected record
   volume, full-text cap, wall/budget ceilings from the brief\'s limits).
5. Persist the plan as YAML + a Run row in state ``planned`` carrying
   the protocol hash and the effective config snapshot (Phase-1 hook).

The interrupted-run rule (spec section 5): changing the brief or the
config produces a NEW run revision, never a silent mutation of prior
evidence. Every ``review plan`` call therefore creates its own Run row;
the revision number counts prior planned runs with the same question,
so an unchanged brief + config yields the same protocol hash and the
prior run stays byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from pathlib import Path
from typing import Any, Mapping

import yaml

from papercli.agent_contract import load_schema, validate
from papercli.providers.identifiers import parse_identifier
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "BriefError",
    "load_brief",
    "validate_brief",
    "apply_brief_defaults",
    "expand_plan",
    "estimate_resources",
    "protocol_hash",
    "build_protocol",
    "write_plan_yaml",
    "create_review_run",
    "plan_review",
    "BRIEF_REQUIRED_FIELDS",
]

#: Spec section 7 required brief fields.
BRIEF_REQUIRED_FIELDS = ("question", "purpose", "scope", "outputs", "limits")

#: Documented defaults for optional fields (recorded when used).
OPTIONAL_DEFAULTS: dict[str, Any] = {
    "review_type": "narrative",
    "date_range": {"from": None, "to": None},
    "languages": ["en"],
    "include_preprints": True,
    "seed_identifiers": [],
    "inclusion_criteria": [],
    "exclusion_criteria": [],
    "themes_to_investigate": [],
    "citation_style": "ieee",
}

#: Providers the deterministic plan targets (Phase-2 registry names).
PLAN_PROVIDERS = ("crossref", "openalex", "arxiv", "s2")


class BriefError(Exception):
    """A brief that cannot be planned (preflight failure -> exit 2)."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def load_brief(path: Path | str) -> dict[str, Any]:
    """Read a brief file. YAML content (the spec brief IS YAML); the file
    extension (``.md`` in the spec example) is not significant."""
    p = Path(path)
    if not p.exists():
        raise BriefError("brief", f"brief file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise BriefError("brief", f"brief is not parseable YAML: {e}") from e
    if not isinstance(data, dict):
        raise BriefError("brief", "brief must be a YAML mapping")
    return data


def _brief_field_from_error(e: str) -> str:
    """The brief field named by a validator error message.

    Two formats: the structural check's
    ``missing required key: 'question'`` and jsonschema's
    ``[path]: message`` (a missing top-level key has the empty path
    and the key name lives in the message: ``'question' is a required
    property``). Returns a dotted field name like ``limits.max_model_calls``.
    """
    m2 = re.match(r"^missing required key: '?\w+'?", e)
    if m2:
        return m2.group(0).split(":")[1].strip().strip(string.punctuation)
    br = re.search(r'\[([^\]]*)\]\s*:?,?', e)
    path_parts = []
    if br and br.group(1).strip():
        path_parts = [
            s.strip().strip(string.punctuation)
            for s in br.group(1).split(",") if s.strip()
        ]
    req = re.search(r"\(?\[?['\"]([\w_]+)['\"]\s+is a required property", e)
    if req:
        return req.group(1)
    return ".".join(path_parts) if path_parts else "brief"

def validate_brief(brief: Mapping[str, Any]) -> None:
    """Validate against the Phase-1 brief schema + spec section 7 rules.

    Raises :class:`BriefError` naming the first missing/invalid field.
    """
    errors = validate(dict(brief), "brief", full=True)
    if errors:
        field = _brief_field_from_error(errors[0])
        raise BriefError(field, errors[0]) from None
    # Spec section 7 semantic preflight (beyond the JSON schema).
    for f in BRIEF_REQUIRED_FIELDS:
        v = brief.get(f)
        if f == "question":
            if not (isinstance(v, str) and v.strip()):
                raise BriefError("question", "required, non-empty research question")
        elif f in ("purpose", "scope"):
            if v is None or (isinstance(v, str) and not v.strip()):
                raise BriefError(f, "required; essential scope is missing "
                                   "(spec section 7 preflight)")
        elif f == "outputs":
            if not (isinstance(v, list) and v):
                raise BriefError("outputs", "required: at least one desired output")
        elif f == "limits":
            if not (isinstance(v, Mapping) and v):
                raise BriefError("limits",
                                 "required: resource limits must be explicitly set "
                                 "before unattended reasoning")
    limits = brief.get("limits") or {}
    for k in ("wall_minutes", "max_search_queries", "max_discovered_records",
              "max_full_text_papers", "max_model_calls", "max_input_tokens",
              "max_output_tokens", "max_estimated_cost_usd"):
        if k not in limits:
            raise BriefError(f"limits.{k}", "required resource limit")
    # Spec section 7: a null monetary cap is allowed only when hard
    # token/call ceilings are set.
    if limits.get("max_estimated_cost_usd") is None:
        if not (isinstance(limits.get("max_model_calls"), int)
                and isinstance(limits.get("max_input_tokens"), int)):
            raise BriefError(
                "limits.max_estimated_cost_usd",
                "null monetary cap requires hard max_model_calls and "
                "max_input_tokens ceilings (spec section 7)")


def apply_brief_defaults(brief: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fill optional fields from the documented defaults; return the
    completed brief + the list of fields a default filled (recorded in
    the saved protocol, spec section 7)."""
    out = dict(brief)
    used: list[str] = []
    for key, default in OPTIONAL_DEFAULTS.items():
        if key not in out or out.get(key) in (None,):
            if key in out:
                pass
            out[key] = (list(default) if isinstance(default, list)
                        else (dict(default) if isinstance(default, dict) else default))
            used.append(key)
    out["schema_version"] = "1.0"
    return out, used


def _date_range_filters(brief: Mapping[str, Any]) -> tuple[str | None, str | None]:
    dr = brief.get("date_range") or {}
    lo = str(dr.get("from") or "").strip() or None
    hi = str(dr.get("to") or "").strip() or None
    return lo, hi


def _seed_queries(brief: Mapping[str, Any]) -> list[str]:
    """Deterministic query seeds: the question plus any themes."""
    seeds = [str(brief.get("question")).strip()]
    for theme in brief.get("themes_to_investigate") or []:
        seeds.append(str(theme).strip())
    return [s for s in seeds if s]


def expand_plan(brief: Mapping[str, Any], providers: list[str] | None = None) -> list[dict[str, Any]]:
    """Database-specific query variants per provider, WITHOUT a model.

    Each variant carries ``query``, ``filters``, ``date_range`` and the
    ``include_preprints`` flag. Query seeds are bounded by
    ``limits.max_search_queries``; the truncation is recorded (it is a
    resource decision, not a silent one).
    """
    providers = providers or list(PLAN_PROVIDERS)
    limits = dict(brief.get("limits") or {})
    max_queries = int(limits.get("max_search_queries") or 30)
    seeds = _seed_queries(brief)

    lo, hi = _date_range_filters(brief)
    include_preprints = bool(brief.get("include_preprints", True))

    variants: list[dict[str, Any]] = []
    budget = 0
    for seed in seeds:
        for prov in providers:
            variant: dict[str, Any] = {
                "provider": prov,
                "query": seed,
                "filters": {},
                "include_preprints": include_preprints,
                "date_range": {"from": lo, "to": hi},
            }
            if prov == "crossref":
                # Crossref REST filters (verified provider facts).
                if lo:
                    variant["filters"]["from-pub-date"] = lo
                if hi:
                    variant["filters"]["until-pub-date"] = hi
                if not include_preprints:
                    variant["filters"]["type"] = "journal-article"
            elif prov == "openalex":
                f: list[str] = []
                if lo:
                    f.append(f"from_publication_date:{lo}")
                if hi:
                    f.append(f"to_publication_date:{hi}")
                if f:
                    variant["filters"]["filter"] = ",".join(f)
            elif prov == "arxiv":
                if not include_preprints:
                    variant["filters"]["include_preprints"] = False
            # s2: enrichment-only (graceful degrade); no special filters.
            variants.append(variant)
            budget += 1
    truncated = 0
    kept = variants[:max_queries]
    truncated = len(variants) - len(kept)
    if truncated:
        kept.append({
            "provider": "(truncated)",
            "query": None,
            "note": (f"query expansion stopped at the "
                     f"max_search_queries={max_queries} ceiling; "
                     f"{truncated} further variant(s) not planned"),
        })
    return kept


def estimate_resources(brief: Mapping[str, Any],
                       query_variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic resource estimate from the brief\'s limits."""
    limits = dict(brief.get("limits") or {})
    planned = [v for v in query_variants if v.get("provider") != "(truncated)"]
    n_queries = len(planned)
    per_query = int(limits.get("records_per_query_estimate") or 50)
    return {
        "expected_queries": n_queries,
        "expected_records": n_queries * per_query,
        "full_text_cap": int(limits.get("max_full_text_papers") or 0),
        "records_ceiling": int(limits.get("max_discovered_records") or 0),
        "wall_ceiling_minutes": limits.get("wall_minutes"),
        "model_call_ceiling": limits.get("max_model_calls"),
        "input_token_ceiling": limits.get("max_input_tokens"),
        "output_token_ceiling": limits.get("max_output_tokens"),
        "cost_ceiling_usd": limits.get("max_estimated_cost_usd"),
        "note": ("Estimates only -- ceilings are not promised throughput "
                 "(spec section 7)."),
    }


def protocol_hash(plan: Mapping[str, Any]) -> str:
    """Stable SHA-256 of the protocol content (deterministic ordering)."""
    return hashlib.sha256(
        json.dumps(dict(plan), sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def build_protocol(brief: Mapping[str, Any],
                   providers: list[str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """The full validated protocol. Returns (protocol, assumptions/notes)."""
    completed, defaults_used = apply_brief_defaults(dict(brief))
    variants = expand_plan(completed, providers)
    limits = dict(completed.get("limits") or {})

    assumptions: list[str] = []
    if defaults_used:
        assumptions.append(
            "defaults applied and recorded for: " + ", ".join(defaults_used))
    if not completed.get("seed_identifiers"):
        assumptions.append("no seed identifiers supplied; discovery starts "
                           "from free-text queries only")
    if not completed.get("inclusion_criteria"):
        assumptions.append("no inclusion criteria supplied; the deterministic "
                           "screening layer will queue all records as uncertain "
                           "unless a filter signal resolves them")
    if not completed.get("exclusion_criteria"):
        assumptions.append("no exclusion criteria supplied; ambiguous records "
                           "rely on year/language filter rechecks only")
    dr = completed.get("date_range") or {}
    if dr.get("from") or dr.get("to"):
        assumptions.append(f"date range applied: from={dr.get('from')} to={dr.get('to')}")
    else:
        assumptions.append("no date range; full history is searched (coverage "
                           "may be lower than a bounded window)")
    if not completed.get("include_preprints", True):
        assumptions.append("preprints excluded from search variants "
                           "(reports only)")
    ambiguities: list[str] = []
    scope = completed.get("scope")
    if isinstance(scope, str) and len(scope.strip()) < 12:
        ambiguities.append("scope is very short; provider coverage may not "
                           "match the intended domain (spec section 8: choose "
                           "coverage by domain)")
    if not completed.get("themes_to_investigate"):
        ambiguities.append("no themes supplied; synthesis organization "
                           "(Phase 5/6) will be question-driven only")

    plan: dict[str, Any] = {
        "schema_version": "1.0",
        "question": completed["question"],
        "purpose": completed.get("purpose"),
        "review_type": completed.get("review_type"),
        "scope": scope,
        "date_range": dr,
        "languages": list(completed.get("languages") or []),
        "include_preprints": bool(completed.get("include_preprints", True)),
        "seed_identifiers": list(completed.get("seed_identifiers") or []),
        "inclusion_criteria": list(completed.get("inclusion_criteria") or []),
        "exclusion_criteria": list(completed.get("exclusion_criteria") or []),
        "themes_to_investigate": list(completed.get("themes_to_investigate") or []),
        "outputs": list(completed.get("outputs") or []),
        "citation_style": completed.get("citation_style"),
        "queries": variants,
        "resource_estimate": estimate_resources(completed, variants),
        "assumptions": assumptions,
        "ambiguities": ambiguities,
        "expected_coverage": (
            f"{len([v for v in variants if v.get('provider') != '(truncated)'])} "
            f"planned query variant(s) across providers "
            f"{sorted({v['provider'] for v in variants if v.get('provider') != '(truncated)'} or ['crossref', 'openalex', 'arxiv', 's2'])}; "
            "estimates only, not promised coverage"),
        "defaults_applied": defaults_used,
        "limits": limits,
        "seed_identifiers_checked": [
            parse_identifier(str(s))["kind"] for s in completed.get("seed_identifiers") or []
        ],
    }
    plan["protocol_hash"] = protocol_hash({k: v for k, v in plan.items()
                                           if k != "protocol_hash"})
    return plan, assumptions


def write_plan_yaml(plan: Mapping[str, Any], out_path: Path | str,
                    plan_id: str, brief_ref: Mapping[str, Any],
                    config_snapshot_ref: str) -> Path:
    """Write the saved plan YAML (with plan_id + refs) and return the path."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = dict(plan)
    doc["plan_id"] = plan_id
    doc["brief_ref"] = dict(brief_ref)
    doc["config_snapshot_ref"] = config_snapshot_ref
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def create_review_run(store: PaperStore, plan: Mapping[str, Any],
                      cfg: Any, env: Mapping[str, str] | None = None,
                      ) -> tuple[str, int, str]:
    """Create the planned Run row + persist the config snapshot.

    Returns ``(run_id, revision, config_snapshot_ref)``. The revision is
    the 1-based ordinal of planned runs sharing this question -- a
    changed brief or config is a NEW revision, never a mutation of a
    prior run (spec section 5).
    """
    snap = cfg.snapshot()
    snap_dir = store.db_path.parent / "config-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    # Content-addressed snapshot file: identical effective config reuses the
    # same file, different config -> different file (no silent mutation).
    digest = hashlib.sha256(
        json.dumps(snap, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    snap_path = snap_dir / f"config-{digest}.json"
    if not snap_path.exists():
        snap_path.write_text(
            json.dumps(snap, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
    snap_ref = f"config-snapshots/config-{digest}.json"

    revision = _next_revision(store, plan)
    limits = dict(plan.get("limits") or {})
    run_id = store.create_run(
        limits=limits,
        protocol_hash=str(plan.get("protocol_hash", "")),
        config_snapshot_ref=snap_ref,
        state="planned",
    )
    with store._conn:
        store._conn.execute(
            "UPDATE runs SET protocol_revision = ? WHERE id = ?",
            (revision, run_id),
        )
    store.save_run_plan(run_id, plan)
    return run_id, revision, snap_ref


def _next_revision(store: PaperStore, plan: Mapping[str, Any]) -> int:
    """The new run's protocol revision ordinal.

    Revision N+1 when N prior planned runs share this question -- every
    ``review plan`` call creates a NEW row, and a changed brief or config
    therefore always yields a *new* revision, never a mutation of the
    prior run (spec section 5: "Changed briefs/configuration require a new
    run revision, not silent mutation"). The just-created row is excluded
    (its plan_json is empty at count time), so a changed brief gets the
    next ordinal while the stored prior runs keep their revision.
    """
    q = str(plan.get("question", "")).strip()
    prior = 0
    for r in store._conn.execute(
            "SELECT plan_json FROM runs WHERE plan_json IS NOT NULL "
            "ORDER BY created_at").fetchall():
        try:
            pj = json.loads(r[0] or "{}")
        except (TypeError, ValueError):
            continue
        if str(pj.get("question", "")).strip() == q:
            prior += 1
    return prior + 1


def plan_review(brief_path: Path | str, out_path: Path | str, *,
                store: PaperStore, cfg: Any, env: Mapping[str, str] | None = None,
                providers: list[str] | None = None,
                brief_content: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """End-to-end ``review plan``. Returns the envelope body.

    Raises :class:`BriefError` on preflight failures (the CLI maps it to
    exit 2 with the field named).
    """
    brief = brief_content if brief_content is not None else load_brief(brief_path)
    validate_brief(brief)

    plan, assumptions = build_protocol(brief, providers)
    plan_id = store.new_id("plan")
    brief_ref: dict[str, Any] = {
        "path": str(Path(brief_path).resolve()),
        "sha256": hashlib.sha256(
            json.dumps(dict(brief), sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()[:16],
    }
    if brief_content is not None:
        brief_ref["note"] = "brief content supplied by caller"

    run_id, revision, snap_ref = create_review_run(store, plan, cfg, env)
    brief_ref["run_id"] = run_id
    # Write the YAML carrying the plan_id + refs, then persist the same
    # content on the Run row (single source of truth: the saved protocol).
    path = write_plan_yaml(plan, out_path, plan_id, brief_ref, snap_ref)
    saved = {**plan, "plan_id": plan_id, "brief_ref": brief_ref,
             "protocol_revision": revision}
    store.save_run_plan(run_id, saved)
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "run_state": "planned",
        "protocol_hash": plan.get("protocol_hash"),
        "protocol_revision": revision,
        "plan_path": str(path),
        "plan": plan,
        "assumptions": assumptions,
    }
