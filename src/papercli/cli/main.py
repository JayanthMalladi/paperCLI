"""Top-level ``papers`` console entry point.

Built with stdlib ``argparse`` for zero extra runtime deps. Windows-first:
no POSIX-only calls, no shell interpolation, pathlib throughout.

Every command emits exactly one JSON envelope on stdout (the
``--help``/``--version``/``version`` commands are the documented
human-readable exception). Diagnostics go to stderr.

Exit codes (spec section 6):
  0   command succeeded
  1   unexpected/internal failure
  2   input/configuration/preflight failure / NOT_IMPLEMENTED
  3   reserved (removed legacy verification code)
  4   retrieval unavailable / no matching result
  5   network, access, or provider rate-limit error
  6   batch or foreground run completed partially
  7   run paused / limit reached
  130 interrupted
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from papercli import __version__
from papercli.cli.envelope import emit, error, ok
from papercli.cli.errors import (
    CliError,
    ConfigError,
    EXIT_CODES,
    InternalError,
    NetworkError,
    NotImplemented as NotImplementedErr,
    RetrievalUnavailable,
)
from papercli.agent_contract import schema_names, validate_envelope

__all__ = ["build_parser", "main", "cli_entry"]


# ---------------------------------------------------------------------------
# NOT_IMPLEMENTED command definitions (Phase 1 stubs)
# ---------------------------------------------------------------------------

#: Declarative NOT_IMPLEMENTED stub table (Phase 1).
#
# key = command path as it appears in the spec contract table. Each value:
#   phase       -> which build phase will implement it
#   description -> what the command will do (documented for the agent)
#   positional  -> positional argument names (absorbed, unused in Phase 1)
#   options     -> documented option flags (absorbed, unused in Phase 1)
#
# Declaring the documented arguments keeps `argparse` from erroring on them,
# so every contract command parses and emits the NOT_IMPLEMENTED envelope
# (exit 2) instead of a usage error.
_NOT_IMPL: dict[str, dict] = {

    "review run": {"phase": "Phase 6", "desc": "Launch a review run (foreground or detached)",
                   "pos": [], "opt": ["--plan", "--detach"]},
    "review status": {"phase": "Phase 6", "desc": "Query run status",
                      "pos": ["run_id"], "opt": []},
    "review logs": {"phase": "Phase 6", "desc": "Stream run logs",
                    "pos": ["run_id"], "opt": ["--follow"]},
    "review resume": {"phase": "Phase 6", "desc": "Resume an interrupted run",
                      "pos": ["run_id"], "opt": ["--detach"]},
    "review cancel": {"phase": "Phase 6", "desc": "Cancel a running review",
                      "pos": ["run_id"], "opt": []},
    "review export": {"phase": "Phase 7", "desc": "Export the output bundle for a completed run",
                      "pos": ["run_id"], "opt": ["--formats"]},
    # "citations audit" is live since Phase 5 (excluded via _implemented_cmds);
    # the entry is kept only so help text stays available if re-versioned.
    "citations audit": {"phase": "Phase 5 (live)", "desc": "Run citation and claim-support audit",
                        "pos": [], "opt": ["--run"]},
    "skill export": {"phase": "Phase 7", "desc": "Export the portable agent skill folder",
                     "pos": [], "opt": ["--out"]},
}

#: Documented option flags that take NO value (action="store_true").
FLAGS = {"--detach", "--follow", "--json"}



def _not_implemented(cmd_path: str, args: argparse.Namespace) -> None:
    spec = _NOT_IMPL.get(cmd_path, {"phase": "Phase 2+", "desc": "Not yet implemented"})
    env = error(
        "NOT_IMPLEMENTED",
        message=f"'{cmd_path}' is not implemented in Phase 1 (planned: {spec['phase']}). {spec['desc']}.",
        retryable=False,
        details={
            "command": cmd_path,
            "planned_phase": spec["phase"],
            "description": spec["desc"],
        },
    )
    emit(env)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Implemented commands
# ---------------------------------------------------------------------------

def _cmd_init(args: argparse.Namespace) -> None:
    from papercli.config.templates import BRIEF_TEMPLATE, CONFIG_TEMPLATE, ENV_TEMPLATE
    from papercli.cli.redact import collect_process_secrets

    cwd = Path.cwd()
    pdir = cwd / ".papercli"
    pdir.mkdir(exist_ok=True)
    config_path = pdir / "config.toml"
    brief_path = pdir / "brief.yaml"

    created: list[str] = []
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
        created.append(str(config_path))
    if not brief_path.exists():
        brief_path.write_text(BRIEF_TEMPLATE, encoding="utf-8", newline="\n")
        created.append(str(brief_path))

    env_path = cwd / ".env"
    env_created = False
    if not env_path.exists() and getattr(args, "env", False):
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8", newline="\n")
        created.append(str(env_path))
        env_created = True

    env_out = ok(
        data={
            "created": created,
            "config_path": str(config_path),
            "brief_path": str(brief_path),
            "env_created": env_created,
            "env_path": str(env_path) if env_created else None,
            "next_steps": [
                "Fill in .papercli/config.toml with your real contact email.",
                "Copy the .env template into .env (git-ignored) with real secret values.",
                "Run: papers doctor --json",
            ],
        },
    )
    emit(env_out)


def _cmd_config_show(args: argparse.Namespace) -> None:
    from papercli.config.loader import Config
    from papercli.cli.redact import collect_process_secrets, mask_value

    cfg = Config.load(cli_overrides={}, cwd=Path.cwd())
    redactor = collect_process_secrets(extra=cfg.env)

    def _redact_value(key_path: str, value: Any) -> Any:
        # Mask any value that looks like a resolved secret.
        if isinstance(value, str) and value:
            return redactor.mask(value)
        return value

    redacted_data: dict[str, Any] = {}
    for section, body in cfg.data.items():
        if isinstance(body, dict):
            redacted_data[section] = {k: _redact_value(f"{section}.{k}", v) for k, v in body.items()}
        else:
            redacted_data[section] = _redact_value(section, body)

    env = ok(
        data={
            "config": redacted_data,
            "sources": cfg.sources,
            "redacted": bool(getattr(args, "redact", False)),
        },
    )
    emit(env)


def _cmd_doctor(args: argparse.Namespace) -> None:
    from papercli.cli.doctor import run_doctor

    result = run_doctor(args)
    emit(result)


def _cmd_schema(args: argparse.Namespace) -> None:
    from papercli.agent_contract.schemas import load_schema, list_schemas

    if args.list:
        env = ok(data={"schemas": list_schemas()})
        emit(env)
        return

    if args.validate:
        # Read a JSON file from --input or stdin and validate it.
        import json as _json
        data: Any
        if args.input:
            try:
                with open(args.input, encoding="utf-8") as f:
                    data = _json.load(f)
            except FileNotFoundError as e:
                env = error(
                    "INPUT_FILE_NOT_FOUND",
                    message=f"Input file not found: {args.input}",
                    retryable=False,
                    details={"input": args.input, "exception": type(e).__name__},
                )
                emit(env)
                sys.exit(2)
            except OSError as e:
                env = error(
                    "INPUT_FILE_ERROR",
                    message=f"Could not read input file: {args.input}",
                    retryable=False,
                    details={"input": args.input, "exception": type(e).__name__},
                )
                emit(env)
                sys.exit(2)
            except _json.JSONDecodeError as e:
                env = error(
                    "INPUT_INVALID_JSON",
                    message=f"Input is not valid JSON: {args.input}",
                    retryable=False,
                    details={"input": args.input, "exception": type(e).__name__},
                )
                emit(env)
                sys.exit(2)
        else:
            data = _json.load(sys.stdin)
        schema_name = args.schema_name or "envelope"
        errs = validate_envelope(data) if schema_name == "envelope" else _validate_generic(data, schema_name)
        if errs:
            env = error(
                "SCHEMA_VALIDATION_FAILED",
                message=f"Validation against '{schema_name}' failed.",
                retryable=False,
                details={"schema": schema_name, "errors": errs},
            )
            emit(env)
            sys.exit(2)
        else:
            env = ok(data={"schema": schema_name, "valid": True})
            emit(env)
        return

    # Print all schemas. Per spec section 6 the default stdout is one JSON
    # envelope: the schemas are carried in ``data`` (the --json form is the
    # explicit envelope spelling of the same data).
    out: dict[str, dict] = {}
    for name in schema_names():
        out[name] = load_schema(name)
    emit(ok(data={"schemas": out}))


def _validate_generic(data: Any, schema_name: str) -> list[str]:
    from papercli.agent_contract.schemas import validate as _validate
    return _validate(data, schema_name, full=True)


def _cmd_version(args: argparse.Namespace) -> None:
    """Human-readable exception: prints version text, not a JSON envelope."""
    print(f"papers {__version__} (agent-papercli)")


# ---------------------------------------------------------------------------
# Live provider commands (Phase 2)
# ---------------------------------------------------------------------------

def _load_live_context():
    """Config + env + cache + store for the live provider commands."""
    from papercli.config.loader import Config
    from papercli.providers.cache import RetrievalCache

    cfg = Config.load(cli_overrides={}, cwd=Path.cwd())
    env = dict(cfg.env)
    import os
    for name in ("PAPERS_EMAIL", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    cache_dir = Path.cwd() / ".papercli" / "storage" / "cache"
    cache = RetrievalCache(cache_dir)
    return cfg, env, cache


def _cmd_search(args: argparse.Namespace) -> None:
    from papercli.storage.db import PaperStore

    providers = [p for p in (getattr(args, "providers", None) or "crossref,openalex,arxiv,s2").split(",") if p.strip()]
    limit = int(getattr(args, "limit", 50) or 50)
    cfg, env, cache = _load_live_context()

    with PaperStore() as store:
        run_id = store.create_run()
        from papercli.providers.retrieval import SearchService
        service = SearchService(cfg, env, cache=cache, store=store)
        try:
            result = service.search(args.query, providers, limit=limit)
        except ValueError as e:
            raise ConfigError(str(e), details={"providers": providers}) from e
        except Exception as e:  # noqa: BLE001
            raise NetworkError(f"provider search failed: {e}") from e
        service.persist(result, run_id)
        # A search where every provider failed is still an ok envelope: the
        # data says so via completeness/errors (spec: provider failures are
        # data, not process failures, for the search command surface).
        envelope = ok(
            data={
                "query": result["query"],
                "records": result["records"],
                "record_count": result["record_count"],
                "deduplicated_from": result["deduplicated_from"],
                "completeness": result["completeness"],
                "providers": result["providers"],
                "errors": result["errors"],
                "run_id": run_id,
            },
            warnings=result["errors"],
        )
    emit(envelope)


def _cmd_resolve(args: argparse.Namespace) -> None:
    from papercli.storage.db import PaperStore

    cfg, env, cache = _load_live_context()
    with PaperStore() as store:
        run_id = store.create_run()
        from papercli.providers.retrieval import resolve_identifier
        result = resolve_identifier(args.identifier, cfg, env,
                                    cache=cache, store=store, run_id=run_id)
    envelope = ok(
        data={
            "input": result["input"],
            "identifier": result["identifier"],
            "canonical": result["canonical"],
            "unpaywall": {k: v for k, v in (result.get("unpaywall") or {}).items()
                          if k != "raw"} if isinstance(result.get("unpaywall"), dict) else result.get("unpaywall"),
            "crossref": {k: v for k, v in (result.get("crossref") or {}).items()
                         if k != "record"} if isinstance(result.get("crossref"), dict) else result.get("crossref"),
            "arxiv": result.get("arxiv"),
            "openalex": result.get("openalex"),
            "warnings": result["warnings"],
            "fetched_at": result["fetched_at"],
            "run_id": run_id,
        },
        warnings=result["warnings"],
    )
    emit(envelope)


def _cmd_fetch(args: argparse.Namespace) -> None:
    from papercli.storage.db import PaperStore

    cfg, env, cache = _load_live_context()
    out_dir = Path(getattr(args, "out", None) or cfg.get("retrieval.out_dir", ".papercli/papers"))
    with PaperStore() as store:
        run_id = store.create_run()
        from papercli.providers.retrieval import AccessLevel, fetch_document
        result = fetch_document(args.identifier, cfg, env, out_dir=out_dir,
                                cache=cache, store=store, run_id=run_id)
    status = result["access_level"]
    envelope = ok(
        data={
            "input": result["input"],
            "identifier": result["identifier"],
            "access_level": status,
            "document": result.get("document"),
            "retrieval_order": result.get("retrieval_order"),
            "candidates": result.get("candidates", []),
            "warnings": result["warnings"],
            "fetched_at": result["fetched_at"],
            "run_id": run_id,
        },
        warnings=result["warnings"],
    )
    emit(envelope)
    # Spec: "retrieval unavailable / no matching result" is exit 4, not 0.
    if status == AccessLevel.UNAVAILABLE:
        sys.exit(4)


# ---------------------------------------------------------------------------
# Document parsing commands (Phase 3)
# ---------------------------------------------------------------------------

def _parse_pages_arg(raw: str | None) -> list[int] | None:
    """Parse a ``--pages`` value like ``"1-3"``, ``"2"``, or ``"1,3,5"``.

    Returns a list of 1-based page numbers, or None when the flag is
    absent. Raises ValueError (-> exit 2) on malformed ranges.
    """
    if raw is None:
        return None
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if lo < 1 or hi < lo:
                raise ValueError(f"invalid page range: {part!r}")
            out.extend(range(lo, hi + 1))
        else:
            pg = int(part)
            if pg < 1:
                raise ValueError(f"invalid page number: {part!r}")
            out.append(pg)
    return out or None


def _cmd_read(args: argparse.Namespace) -> None:
    """Extract text + locations + warnings from a local document."""
    from papercli.documents.parser import parse_document, ParseError
    from papercli.providers.cache import detect_document_kind

    path = Path(args.file)
    if not path.exists():
        raise ConfigError(f"document file not found: {args.file}",
                          details={"file": str(args.file)})
    data = path.read_bytes()
    kind = detect_document_kind(data)
    if kind not in ("pdf", "xml", "html"):
        raise RetrievalUnavailable(
            f"unsupported document kind: {kind!r}; PDF/HTML/XML only",
            details={"file": str(path), "detected_kind": kind})

    pages_wanted = _parse_pages_arg(getattr(args, "pages", None))
    max_chars = int(getattr(args, "max_chars", None) or 18000)
    try:
        report = parse_document(data, kind)
    except ParseError as e:
        raise ConfigError(f"document parse failed: {e}",
                          details={"file": str(path), "kind": kind})

    all_sections = report["text_index"]
    if pages_wanted is not None:
        all_sections = [s for s in all_sections if s["page"] in pages_wanted]

    # Page text assembly: stable-location ordered, capped at max_chars.
    selected: list[dict[str, Any]] = []
    budget = 0
    for s in all_sections:
        piece = s["text"]
        if budget + len(piece) > max_chars and selected:
            break
        selected.append(s)
        budget += len(piece)
    truncated = len(selected) < len(all_sections)
    text = "\n\n".join(s["text"] for s in selected)

    warnings = list(report["warnings"])
    if report["scanned"]:
        warnings.append("scanned pages flagged: no text extracted (OCR out of scope)")
    if truncated:
        warnings.append(f"output capped at {max_chars} chars; "
                       f"{len(all_sections) - len(selected)} location(s) omitted")

    emit(ok(
        data={
            "file": str(path),
            "kind": kind,
            "page_count": report["page_count"],
            "requested_pages": pages_wanted,
            "text": text,
            "locations": [
                {"page": s["page"], "section": s["section"],
                 "paragraph": s["paragraph"]} for s in selected
            ],
            "characters": len(text),
            "scanned": report["scanned"],
        },
        warnings=warnings,
    ))


def _cmd_ingest(args: argparse.Namespace) -> None:
    """Import a local PDF/HTML/XML into a run's Document registry."""
    from papercli.documents.ingest import IngestRejected, ingest_local_document
    from papercli.storage.db import PaperStore

    run_id = args.run
    with PaperStore() as store:
        if store.run_row(run_id) is None:
            raise ConfigError(
                f"run not found: {run_id}",
                details={"run_id": run_id},
            )
        # Run-context paper id: use the first paper in the run that carries
        # an identity (doi / arXiv id / title), so the wrong-paper guard has
        # a requested identity to compare. A paper row with no identity is
        # not a usable guard context.
        import json as _json
        paper_id: str | None = None
        requested_identity: dict[str, Any] = {}
        for row in store.papers_for_run(run_id):
            ids = _json.loads(row.get("canonical_ids_json") or "{}")
            cand = {k: v for k, v in {
                "doi": ids.get("doi"),
                "arxiv_id": ids.get("arxiv_id"),
                "title": row.get("title"),
            }.items() if v}
            if cand:
                paper_id = row["id"]
                requested_identity = cand
                break
        if paper_id is None:
            requested_identity = {}
        try:
            report = ingest_local_document(
                store, run_id, args.file, paper_id=paper_id,
                requested_identity=requested_identity,
            )
        except IngestRejected as e:
            raise RetrievalUnavailable(
                e.reason, details={"file": str(Path(args.file)),
                                   **e.details})
        # Re-read the warnings (ingest adds identity-flag warnings after the
        # document row exists).
        warnings = list(report.get("parse_warnings") or [])
        doc = store.document_row(report["document_id"])
        if doc and doc.get("identity_status") == "inconclusive":
            warnings = warnings or []
            warnings.append("wrong-paper guard: identity could not be confirmed "
                            "(inconclusive); document flagged")
        # D-1 disclosure: surface the parse-quality grade + the capped
        # effective access level so the envelope never represents a
        # scanned/partial document as fully read.
        data = {
            "run_id": run_id,
            "document_id": report["document_id"],
            "paper_id": report.get("paper_id") or paper_id,
            "kind": report["kind"],
            "content_hash": report["content_hash"],
            "access_status": report["access_status"],
            "access_level": report["access_level"],
            "parse_grade": report["parse_grade"],
            "page_count": report["page_count"],
            "sections_stored": report["sections_stored"],
            "scanned": report["scanned"],
            "identity_status": report["identity_status"],
            "identity": report.get("identity"),
        }
        if report["parse_grade"] not in ("good",):
            data["missing_text_disclosure"] = (
                f"parse_quality={report['parse_grade']}; the document is "
                f"represented at access level {report['access_level']}, "
                "not full text (OCR is out of scope)")
        emit(ok(data=data, warnings=warnings))


# ---------------------------------------------------------------------------
# Research planning + task protocol (Phase 4)
# ---------------------------------------------------------------------------

def _cmd_review_plan(args: argparse.Namespace) -> None:
    """``papers review plan --brief <file> --out review-plan.yaml``.

    Validates the brief against the Phase-1 schema (preflight failures are
    exit 2 with the missing field named), expands a deterministic
    database-specific query plan (no model calls), writes the plan YAML,
    and creates the planned Run row with protocol hash + config snapshot.
    A changed brief or config creates a NEW run revision -- never a
    silent mutation of a prior run (spec section 5).
    """
    from papercli.config.loader import Config
    from papercli.research.plan import BriefError, plan_review
    from papercli.storage.db import PaperStore

    cfg = Config.load(cli_overrides={}, cwd=Path.cwd())
    env = dict(cfg.env)
    import os as _os
    for name in ("PAPERS_EMAIL", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"):
        if _os.environ.get(name):
            env[name] = _os.environ[name]

    out_path = getattr(args, "out", None) or "review-plan.yaml"
    try:
        with PaperStore() as store:
            result = plan_review(
                args.brief, out_path, store=store, cfg=cfg, env=env)
        env_out = ok(
            data={
                "plan_id": result["plan_id"],
                "run_id": result["run_id"],
                "run_state": result["run_state"],
                "protocol_hash": result["protocol_hash"],
                "protocol_revision": result["protocol_revision"],
                "plan_path": result["plan_path"],
                "resource_estimate": result["plan"]["resource_estimate"],
                "queries": len([q for q in result["plan"]["queries"]
                                if q.get("provider") != "(truncated)"]),
                "assumptions": result["assumptions"],
                "ambiguities": result["plan"].get("ambiguities", []),
                "defaults_applied": result["plan"].get("defaults_applied", []),
            },
            warnings=result["plan"].get("ambiguities") or [],
        )
        emit(env_out)
    except BriefError as e:
        env_out = error(
            "CONFIG_REQUIRED",
            message=f"brief preflight failed: {e.message}",
            retryable=False,
            details={"field": e.field, "brief": str(Path(args.brief))},
        )
        emit(env_out)
        sys.exit(2)




# ---------------------------------------------------------------------------
# Review lifecycle commands (Phase 6 batch A-3: detach / recovery / CLI)
# ---------------------------------------------------------------------------

#: The Phase-6 worker-child module override (offline test seam -- the
#: production default is the real worker; see fake_worker_child.py).
_WORKER_MODULE_ENV = "PAPERS_WORKER_MODULE"


def _find_review_run(store, run_id_or_plan: str | None = None,
                    plan: str | None = None):
    """Resolve a run id: the literal run id, or the run a PLAN names
    (``plan_id`` -> its created Run row). Returns (run_id, row)."""
    row = store.run_row(run_id_or_plan) if run_id_or_plan else None
    if row is not None:
        return str(row["id"]), row
    # Fall back to a plan file: its plan_id names the run it created.
    if plan:
        import yaml
        try:
            doc = yaml.safe_load(Path(plan).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(
                f"cannot read plan file '{plan}': {e}",
                details={"plan": plan})
        pid = (doc or {}).get("plan_id")
        if pid:
            row = store.find_run_by_plan_id(str(pid))
            if row is not None:
                return str(row["id"]), row
    return None, None


def _cmd_review_run(args: argparse.Namespace) -> None:
    """``papers review run --plan review-plan.yaml --detach``.

    Validates the plan, ensures the Run row, launches the detached
    worker, and reports "accepted" ONLY when the child handshaked +
    acquired its lock (spec section 6) -- not when the review completed.
    Exit 0 accepted; exit 2 when it cannot start (bad plan / lock held
    / changed brief)."""
    from papercli.jobs.detach import LaunchError, launch_worker

    plan_path = Path(args.plan)
    if not plan_path.exists():
        _emit_review_error(
            "CONFIG_REQUIRED",
            f"plan file not found: {plan_path}",
            {"plan": str(plan_path)})
    import yaml
    try:
        doc = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        _emit_review_error(
            "CONFIG_REQUIRED", f"invalid plan YAML: {e}",
            {"plan": str(plan_path)})

    # Preflight: a plan must carry the fields the worker needs (query
    # plan + limits). A plan whose brief has changed is NOT silently
    # re-used -- resume/plan make the revision explicit (spec section 5).
    limits = (doc or {}).get("limits") or {}
    queries = [q for q in ((doc or {}).get("queries") or [])
               if q.get("provider") != "(truncated)"]
    if not limits or not queries:
        _emit_review_error(
            "CONFIG_REQUIRED",
            "plan has no limits or no usable queries; regenerate it with "
            "`papers review plan`",
            {"plan": str(plan_path)})

    from papercli.storage.db import PaperStore
    with PaperStore() as store:
        run_id, row = _find_review_run(store, plan=str(plan_path))
        if row is None:
            # No run row yet for this plan: create the planned Run.
            pid = str((doc or {}).get("plan_id") or store.new_id("plan"))
            from papercli.research.plan import protocol_hash
            # A plan file is the validated protocol itself; keep its
            # protocol hash (or derive it from the plan content).
            plan = dict(doc)
            plan.setdefault("protocol_hash", protocol_hash(plan))
            limits = dict(plan.get("limits") or {})
            run_id = store.create_run(
                limits=limits, state="running",
                protocol_hash=str(plan.get("protocol_hash", "")))
            import json as _json
            with store._conn:
                store._conn.execute(
                    "UPDATE runs SET plan_json=? WHERE id=?",
                    (_json.dumps({**plan, "plan_id": pid}, sort_keys=True),
                     run_id))
            row = store.run_row(run_id)
        # Launch: the child runs in the project's own interpreter; the
        # offline test seam swaps the module via the env var.
        try:
            res = launch_worker(
                store, run_id, detach=args.detach,
                detach_timeout_seconds=(5.0 if args.detach else 30.0))
        except LaunchError as e:
            _emit_review_error(
                e.code, e.message, {"run_id": run_id, **e.details})
        # "accepted" = the child handshaked + acquired its lock (the
        # review has NOT completed -- spec section 6). The run row moves
        # planned/queued -> running now that a live worker holds it. The
        # UPDATE is a CAS on the current state: a child that already
        # closed the run (terminal state) is NEVER clobbered back to
        # 'running' (a fast worker finishes before this write lands).
        with store._conn:
            store._conn.execute(
                "UPDATE runs SET state='running' WHERE id=? AND state IN "
                "('planned','queued')", (run_id,))
        row = store.run_row(run_id)
        if args.detach:
            ok_env = ok(data={
                "run_id": run_id,
                "state": row.get("state"),
                "accepted": True,
                "worker_pid": res.pid,
                "detach": True,
                "lock_file": str(res.lock_file) if res.lock_file else None,
                "handshake": bool(res.handshake),
            })
            emit(ok_env)
            return
        # Foreground: the command BLOCKS until the worker closes, then
        # reports the final state. A partial/failed closure is exit 6
        # (spec section 6: batch/foreground run completed partially).
        final_state = str((store.run_row(run_id) or {}).get("state")
                          or row.get("state"))
        ok_env = ok(data={
            "run_id": run_id,
            "state": final_state,
            "accepted": True,
            "worker_pid": res.pid,
            "detach": False,
            "handshake": bool(res.handshake),
            "worker_exit_code": res.exit_code,
        })
        emit(ok_env)
        if final_state in ("partial", "failed"):
            sys.exit(6)


def _cmd_review_status(args: argparse.Namespace) -> None:
    """``papers review status RUN_ID``.

    Run state + per-task-type progress + heartbeat age + STALE flag +
    budget usage-vs-limits + failures. A successful status call CAN
    describe a failed/partial run (spec section 6)."""
    import json as _json
    from papercli.jobs.detach import (
        STALE_AFTER_SECONDS, heartbeat_age_seconds, lock_held)
    from papercli.jobs.tasks import TaskQueue
    from papercli.storage.db import PaperStore

    with PaperStore() as store:
        row = store.run_row(args.run_id)
        if row is None:
            _emit_review_error("RUN_NOT_FOUND",
                               f"run not found: {args.run_id}",
                               {"run_id": args.run_id})
        tasks = TaskQueue(store).list(args.run_id)
        by_type: dict[str, dict[str, int]] = {}
        for t in tasks:
            d = by_type.setdefault(t.type, {})
            d[t.state] = d.get(t.state, 0) + 1
        counts = {
            "total": len(tasks),
            "succeeded": sum(1 for t in tasks if t.state == "succeeded"),
            "failed": sum(1 for t in tasks if t.state == "failed"),
            "unfinished": sum(1 for t in tasks
                              if t.state in ("pending", "leased", "running",
                                             "retry_wait")),
        }
        hb_age = heartbeat_age_seconds(store, args.run_id)
        held = lock_held(store, args.run_id)
        stale = bool(hb_age is not None
                     and hb_age > STALE_AFTER_SECONDS)
        # Budget block (same restore as the run summary).
        counters = _json.loads(row.get("counters_json") or "{}")
        budget = counters.get("worker", {}).get("budget") or {}
        limits = _json.loads(row.get("limits_json") or "{}")
        from papercli.exports.run_summary import build_run_summary
        summary = build_run_summary(store, args.run_id)
        env = ok(data={
            "run_id": args.run_id,
            "state": row.get("state"),
            "task_progress": by_type,
            "task_counts": counts,
            "heartbeat_age_seconds": round(hb_age, 3) if hb_age is not None
                                     else None,
            "lock": {"held": held.get("held"), "stale": held.get("stale"),
                     "holder_pid": held.get("holder_pid"),
                     "reason": held.get("reason")},
            "stale": stale,
            "budget": summary.get("budget"),
            "failures": summary.get("failures"),
            "counts": summary.get("counts"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
        })
        emit(env)


def _cmd_review_logs(args: argparse.Namespace) -> None:
    """``papers review logs RUN_ID --follow``.

    Streams worker.log.jsonl, one JSON envelope per line (spec section 6
    batch/follow form). With --follow it tails the file until the run
    closes or the user interrupts."""
    import json as _json
    import time as _time
    from papercli.exports.run_summary import run_dir
    from papercli.storage.db import PaperStore

    with PaperStore() as store:
        row = store.run_row(args.run_id)
        if row is None:
            _emit_review_error("RUN_NOT_FOUND",
                               f"run not found: {args.run_id}",
                               {"run_id": args.run_id})
        log = run_dir(store, args.run_id) / "worker.log.jsonl"
        if not log.exists():
            emit(ok(data={"run_id": args.run_id, "lines": 0,
                          "note": "no worker log yet"}))
            return
        emitted: list[str] = []
        pos = 0
        while True:
            with log.open("r", encoding="utf-8") as fh:
                fh.seek(pos)
                for line in fh:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    emit_str = _json.dumps(_json.loads(line),
                                        ensure_ascii=False)
                    emitted.append(emit_str)
                pos = fh.tell()
            if not args.follow:
                break
            # A follow stream: one envelope per line on stdout; stop when
            # the run reaches a terminal state and the log stops growing.
            state = (store.run_row(args.run_id) or {}).get("state")
            if state in ("completed", "partial", "failed", "cancelled") and pos > 0:
                break
            _time.sleep(0.5)
        # Re-emit as a single JSON array so stdout stays one envelope
        # (spec section 6: default stdout is one JSON envelope; the follow
        # stream's envelopes are the data).
        emit(ok(data={"run_id": args.run_id, "lines": len(emitted),
                      "events": [_json.loads(l) for l in emitted]}))


def _cmd_review_resume(args: argparse.Namespace) -> None:
    """``papers review resume RUN_ID --detach``.

    The explicit-recovery path: stale detection, idempotent re-queue,
    re-launch. A changed brief/config -> exit 2 with new-revision
    guidance (spec section 5)."""
    from papercli.jobs.detach import BriefChangedError, RunNotStartable
    from papercli.jobs.recovery import resume_run
    from papercli.storage.db import PaperStore

    brief_path = getattr(args, "brief", None)
    brief = None
    if brief_path:
        import yaml
        brief = yaml.safe_load(Path(brief_path).read_text(encoding="utf-8"))

    with PaperStore() as store:
        if store.run_row(args.run_id) is None:
            _emit_review_error("RUN_NOT_FOUND",
                               f"run not found: {args.run_id}",
                               {"run_id": args.run_id})
        try:
            res = resume_run(
                store, args.run_id, brief=brief,
                detach_flag=args.detach,
                detach_timeout_seconds=(5.0 if args.detach else 30.0))
        except BriefChangedError as e:
            _emit_review_error("BRIEF_CHANGED", e.message, e.details)
        except RunNotStartable as e:
            _emit_review_error("RUN_NOT_STARTABLE", e.message, e.details)
        env = ok(data={
            "run_id": res.run_id,
            "state": res.state,
            "resumed": res.resumed,
            "launched": res.launched,
            "worker_pid": res.pid,
            "tasks_released": res.tasks_released,
            "tasks_requeued": res.tasks_requeued,
            "notes": res.notes,
        })
        emit(env)


def _cmd_review_cancel(args: argparse.Namespace) -> None:
    """``papers review cancel RUN_ID``.

    Writes the worker's cancel-request file so the RUNNING worker stops
    new dispatch + checkpoints + writes the partial status bundle (spec
    12). If no worker is running, the run is marked cancelled directly."""
    import json as _json
    from papercli.jobs.detach import lock_held
    from papercli.storage.db import PaperStore

    with PaperStore() as store:
        row = store.run_row(args.run_id)
        if row is None:
            _emit_review_error("RUN_NOT_FOUND",
                               f"run not found: {args.run_id}",
                               {"run_id": args.run_id})
        if row.get("state") in ("completed", "partial", "failed",
                                "cancelled"):
            _emit_review_error(
                "RUN_ALREADY_CLOSED",
                f"run {args.run_id} already ended in state "
                f"'{row.get('state')}'; nothing to cancel",
                {"run_id": args.run_id, "state": row.get("state")})
        held = lock_held(store, args.run_id)
        worker_running = bool(held.get("held")) and not held.get("stale")
        from papercli.exports.run_summary import run_dir
        cancel = run_dir(store, args.run_id) / "cancel-request"
        if worker_running:
            # The running worker polls this file; it stops scheduling,
            # terminates bounded child work where safe, checkpoints, and
            # writes the partial status bundle on its next loop (spec 12).
            cancel.write_text(_json.dumps(
                {"requested_at": _utc_now_iso_cli(),
                 "note": "stop new dispatch; checkpoint + partial bundle"},
                indent=2), encoding="utf-8")
            env = ok(data={
                "run_id": args.run_id, "worker_running": True,
                "cancel_request": str(cancel),
                "note": "the worker will stop dispatch and close a "
                        "partial bundle on its next checkpoint",
            })
            emit(env)
        else:
            # No live worker: release any stale lock + expired leases and
            # mark the run cancelled directly (an interrupted run with no
            # live worker is a partial state, spec section 5).
            from papercli.jobs.recovery import requeue_unfinished
            released, requeued = requeue_unfinished(store, args.run_id)
            store.update_run(args.run_id, state="cancelled",
                             ended_at=_utc_now_iso_cli())
            env = ok(data={
                "run_id": args.run_id, "worker_running": False,
                "state": "cancelled",
                "tasks_released": released, "tasks_requeued": requeued,
            })
            emit(env)


def _cmd_review_export(args: argparse.Namespace) -> None:
    """``papers review export RUN_ID --formats md,pdf,docx,bib,csl,csv``.

    Runs the Phase-6 export surface (structured dumps + run summary).
    Rendered md/pdf/docx are recorded as DEFERRED to Phase 7 in the
    artifact inventory -- never silently missing. Exit 0 on success;
    exit 6 when the run itself is partial (its summary is still
    written)."""
    from papercli.exports.run_summary import (
        build_run_summary, write_structured_dumps, write_run_summary)
    from papercli.storage.db import PaperStore

    formats = [f for f in (getattr(args, "formats", None) or
                           "md,pdf,docx,bib,csl,csv").split(",")
               if f.strip()]
    with PaperStore() as store:
        row = store.run_row(args.run_id)
        if row is None:
            _emit_review_error("RUN_NOT_FOUND",
                               f"run not found: {args.run_id}",
                               {"run_id": args.run_id})
        out = write_structured_dumps(store, args.run_id,
                                     formats=formats)
        state = str(row.get("state") or out.get("state"))
        # The COMMAND succeeded (the bundle was written): exit 0. The
        # run's own state (which may be partial/failed) is reported in the
        # envelope data, never silently dropped (spec section 6).
        env = ok(data={
            "run_id": args.run_id,
            "state": state,
            "written": out.get("written"),
            "deferred": out.get("deferred"),
            "formats_requested": formats,
        })
        emit(env)


def _emit_review_error(code: str, message: str,
                       details: dict | None = None) -> None:
    """Emit a review-lifecycle error envelope and exit 2 (spec section 6:
    input/configuration/preflight failure)."""
    emit(error(code, message, retryable=False,
               details=details or {}))
    sys.exit(2)


def _utc_now_iso_cli() -> str:
    from papercli.storage.db import utc_now_iso
    return utc_now_iso()


def _cmd_task_next(args: argparse.Namespace) -> None:
    """``papers task next --run RUN_ID --worker AGENT_ID`` (D-2 fix).

    Leases the next claimable task via the Phase-3 TaskQueue protocol and
    emits the task payload + lease + expiry. No claimable task is an
    ``ok`` envelope with ``task: null`` (a full queue is not an error).
    """
    from papercli.research.tasks import task_next
    from papercli.storage.db import PaperStore

    run_id = args.run
    worker = args.worker
    with PaperStore() as store:
        if store.run_row(run_id) is None:
            raise ConfigError(f"run not found: {run_id}",
                              details={"run_id": run_id})
        lease_ttl = float(getattr(args, "lease_ttl", None) or 300)
        data = task_next(store, run_id, worker, lease_ttl=lease_ttl)
    emit(ok(data=data))


def _cmd_task_submit(args: argparse.Namespace) -> None:
    """``papers task submit TASK_ID --input result.json`` (D-2 fix).

    Validates the submission against the task queue protocol: distinct
    rejection codes for invalid / stale / source-inconsistent payloads;
    a duplicated payload hash returns the prior result (idempotent).
    """
    from papercli.research.tasks import load_submission_input, task_submit
    from papercli.storage.db import PaperStore

    task_id = args.task_id
    try:
        payload = load_submission_input(args.input)
    except FileNotFoundError as e:
        env_out = error("INPUT_FILE_NOT_FOUND", message=str(e),
                        retryable=False, details={"input": args.input})
        emit(env_out)
        sys.exit(2)
    except ValueError as e:
        env_out = error("INPUT_INVALID_JSON", message=str(e),
                        retryable=False, details={"input": args.input})
        emit(env_out)
        sys.exit(2)

    run_id = getattr(args, "run", None)
    worker = getattr(args, "worker", None)
    if not run_id or not worker:
        # Recover the run/worker from the task row when the flags are
        # absent (the task row is the source of truth for its lease owner).
        with PaperStore() as store:
            task = store.connection.execute(
                "SELECT run_id, lease_owner FROM tasks WHERE id = ?",
                (task_id,)).fetchone()
            if task is None:
                env_out = error(
                    "TASK_NOT_FOUND",
                    message=f"task not found: {task_id} (supply --run/--worker "
                            "or pass --input that belongs to a known task)",
                    retryable=False, details={"task_id": task_id})
                emit(env_out)
                sys.exit(2)
            run_id = run_id or task["run_id"]
            worker = worker or task["lease_owner"] or "anonymous"
            env_out, status = _do_submit(
                store, run_id, task_id, worker, payload)
    else:
        with PaperStore() as store:
            if store.run_row(run_id) is None:
                raise ConfigError(f"run not found: {run_id}",
                                  details={"run_id": run_id})
            env_out, status = _do_submit(
                store, run_id, task_id, worker, payload)
    emit(env_out)
    if status in ("rejected_invalid", "rejected_stale", "rejected_source",
                  "rejected_state"):
        sys.exit(2)
    # accepted / duplicate -> command succeeded (exit 0).


def _do_submit(store, run_id: str, task_id: str, worker: str,
               payload: dict) -> tuple[dict, str]:
    """Run the queue submission and shape the envelope."""
    from papercli.research.tasks import task_submit

    source_input_hash = None
    row = store.connection.execute(
        "SELECT input_hash FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is not None:
        source_input_hash = row["input_hash"]
    data = task_submit(store, run_id, task_id, worker, payload,
                       source_input_hash=source_input_hash)
    status = data["status"]
    if status == "accepted":
        return ok(data=data), status
    if status == "duplicate":
        return ok(data=data, warnings=[data.get("reason")]), status
    code = {
        "rejected_invalid": "TASK_SUBMIT_INVALID",
        "rejected_stale": "TASK_SUBMIT_STALE",
        "rejected_source": "TASK_SUBMIT_SOURCE_MISMATCH",
        "rejected_state": "TASK_SUBMIT_BAD_STATE",
    }[status]
    return error(code, message=data.get("reason", status),
                 retryable=(status in ("rejected_stale", "rejected_state")),
                 details={"task_id": task_id, "payload_hash":
                          data.get("payload_hash"), "status": status}), status


def _cmd_evidence_list(args: argparse.Namespace) -> None:
    """``papers evidence list --run RUN_ID``: the run's evidence records."""
    from papercli.storage.db import PaperStore

    run_id = args.run
    with PaperStore() as store:
        if store.run_row(run_id) is None:
            raise ConfigError(f"run not found: {run_id}",
                              details={"run_id": run_id})
        records = store.evidence_for_run(run_id)
    emit(ok(data={
        "run_id": run_id,
        "count": len(records),
        "evidence": records,
    }))


def _cmd_citations_audit(args: argparse.Namespace) -> None:
    """``papers citations audit --run RUN_ID``: the run's claim-audit
    results + reference-identity checks + coverage/limits disclosure
    (spec section 10), as one JSON envelope."""
    from papercli.citations.audit import audit_citations
    from papercli.storage.db import PaperStore

    run_id = args.run
    with PaperStore() as store:
        if store.run_row(run_id) is None:
            raise ConfigError(f"run not found: {run_id}",
                              details={"run_id": run_id})
        data = audit_citations(store, run_id)
    emit(ok(data=data))


# ---------------------------------------------------------------------------
# Argparse builder
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="papers",
        description="PaperCLI — agentic, evidence-backed literature review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit (human-readable).")
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    # init
    p_init = sub.add_parser("init", help="Create .papercli/ config and brief template.")
    p_init.add_argument("--env", action="store_true", help="Also create a .env template (git-ignored).")
    p_init.set_defaults(func=_cmd_init)

    # config show
    p_cfg = sub.add_parser("config", help="Inspect effective configuration.")
    cfg_sub = p_cfg.add_subparsers(dest="config_sub", metavar="subcommand")
    p_show = cfg_sub.add_parser("show", help="Show merged config with secrets redacted.")
    p_show.add_argument("--redact", action="store_true", default=True,
                        help="Mask secret values (default: on). Pass --no-redact to disable.")
    p_show.add_argument("--no-redact", action="store_true", dest="no_redact",
                        help="Do NOT mask secret values (dangerous).")
    p_show.set_defaults(func=_cmd_config_show)

    # doctor
    p_doc = sub.add_parser("doctor", help="Run preflight checks.")
    p_doc.add_argument("--json", action="store_true", default=True,
                       help="Emit results as a JSON envelope (default).")
    p_doc.add_argument("--storage-dir", default=None, help="Override storage dir for checks.")
    p_doc.add_argument("--skip-network", action="store_true",
                       help="Skip model-endpoint reachability check.")
    p_doc.set_defaults(func=_cmd_doctor)

    # schema
    p_schema = sub.add_parser("schema", help="Print or validate published JSON Schemas.")
    p_schema.add_argument("--list", action="store_true", help="List schema names and required fields.")
    p_schema.add_argument("--validate", action="store_true", help="Validate a JSON instance against a schema.")
    p_schema.add_argument("--schema-name", default="envelope", help="Schema name for --validate.")
    p_schema.add_argument("--input", default=None, help="Path to JSON file for --validate (else stdin).")
    p_schema.add_argument("--json", action="store_true", default=False,
                          help="Explicit envelope form of the schema output (same data as the default).")
    p_schema.set_defaults(func=_cmd_schema)

    # version (explicit subcommand, in addition to --version flag)
    p_ver = sub.add_parser("version", help="Print version (human-readable exception).")
    p_ver.set_defaults(func=_cmd_version)

    # Live provider commands (Phase 2).
    p_search = sub.add_parser(
        "search",
        help="Live provider search: crossref,openalex,arxiv,s2 -> one JSON envelope.",
    )
    p_search.add_argument("query", help="Free-text search query.")
    p_search.add_argument("--providers", default="crossref,openalex,arxiv,s2",
                          help="Comma-separated providers (default: crossref,openalex,arxiv,s2).")
    p_search.add_argument("--limit", default=50, type=str,
                          help="Max results per provider (default: 50).")
    p_search.set_defaults(func=_cmd_search)

    p_resolve = sub.add_parser(
        "resolve",
        help="Resolve a DOI or arXiv identifier to normalized metadata + OA locations.",
    )
    p_resolve.add_argument("identifier", help="DOI (10.x/...) or arXiv id (1605.0000, cond-mat/0207270).")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_fetch = sub.add_parser(
        "fetch",
        help="Run the retrieval order: download, validate, and cache a document.",
    )
    p_fetch.add_argument("identifier", help="DOI or arXiv identifier.")
    p_fetch.add_argument("--out", default=None,
                         help="Output dir for validated documents (default: retrieval.out_dir).")
    p_fetch.set_defaults(func=_cmd_fetch)

    # Document parsing commands (Phase 3).
    p_read = sub.add_parser(
        "read",
        help="Extract text + locations + warnings from a local document "
             "(PDF/HTML/XML).")
    p_read.add_argument("file", help="Path to the local document.")
    p_read.add_argument("--pages", default=None,
                        help="Page selection, e.g. '1-3' or '1,3,5' (default: all).")
    p_read.add_argument("--max-chars", dest="max_chars", default="18000",
                        help="Output character ceiling (default: 18000).")
    p_read.set_defaults(func=_cmd_read)

    p_ingest = sub.add_parser(
        "ingest",
        help="Import a local PDF/HTML/XML into a run's Document registry "
             "(hash + provenance; identity-checked when the run has a paper).")
    p_ingest.add_argument("file", help="Path to the local document.")
    p_ingest.add_argument("--run", required=True,
                          help="Run id to attach the document to.")
    p_ingest.set_defaults(func=_cmd_ingest)

    # Phase 4: implemented research commands (replace the Phase 1 stubs).
    def _ensure_group(group_name: str, help_text: str):
        if group_name not in _group_map:
            gp = sub.add_parser(group_name, help=help_text)
            gp_sub = gp.add_subparsers(
                dest=f"{group_name}_sub", metavar="subcommand")
            _group_map[group_name] = gp_sub
        return _group_map[group_name]

    _implemented_cmds = {"review plan", "task next", "task submit",
                         "evidence list", "citations audit",
                         "review run", "review status", "review logs",
                         "review resume", "review cancel",
                         "review export"}

    review_sub = _ensure_group("review", "Group: review (research pipeline)")
    task_sub = _ensure_group("task", "Group: task (lease-based task protocol)")
    evidence_sub = _ensure_group("evidence", "Group: evidence records")

    p_rplan = review_sub.add_parser(
        "plan", help="Validate a brief into a research protocol + planned Run.")
    p_rplan.add_argument("--brief", required=True,
                        help="Path to the research brief (YAML content).")
    p_rplan.add_argument("--out", default="review-plan.yaml",
                         help="Where to write the plan YAML "
                              "(default: review-plan.yaml).")
    p_rplan.set_defaults(func=_cmd_review_plan)

    # Phase 6 review lifecycle (detach / recovery / status / logs /
    # cancel / export). These are live commands now (batch A-3).
    p_rrun = review_sub.add_parser(
        "run", help="Launch a review run (foreground or detached).")
    p_rrun.add_argument("--plan", required=True,
                        help="Path to the plan YAML (from `papers review plan`).")
    p_rrun.add_argument("--detach", action="store_true", default=False,
                        help="Launch the detached worker; wait for its handshake.")
    p_rrun.set_defaults(func=_cmd_review_run)

    p_rstat = review_sub.add_parser(
        "status", help="Query run status (state, progress, heartbeat, budget).")
    p_rstat.add_argument("run_id", help="Run id.")
    p_rstat.set_defaults(func=_cmd_review_status)

    p_rlogs = review_sub.add_parser(
        "logs", help="Stream run worker logs (one JSON envelope per line).")
    p_rlogs.add_argument("run_id", help="Run id.")
    p_rlogs.add_argument("--follow", action="store_true", default=False,
                         help="Tail the log until the run closes.")
    p_rlogs.set_defaults(func=_cmd_review_logs)

    p_rres = review_sub.add_parser(
        "resume", help="Resume an interrupted run (explicit recovery).")
    p_rres.add_argument("run_id", help="Run id.")
    p_rres.add_argument("--detach", action="store_true", default=False,
                        help="Re-launch the detached worker.")
    p_rres.add_argument("--brief", default=None,
                        help="Path to the current brief (changed-brief check).")
    p_rres.set_defaults(func=_cmd_review_resume)

    p_rcancel = review_sub.add_parser(
        "cancel", help="Cancel a running review (checkpoint + partial bundle).")
    p_rcancel.add_argument("run_id", help="Run id.")
    p_rcancel.set_defaults(func=_cmd_review_cancel)

    p_rexp = review_sub.add_parser(
        "export", help="Export the output bundle for a run.")
    p_rexp.add_argument("run_id", help="Run id.")
    p_rexp.add_argument("--formats", default="md,pdf,docx,bib,csl,csv",
                        help="Comma-separated formats (default: all).")
    p_rexp.set_defaults(func=_cmd_review_export)

    p_tnext = task_sub.add_parser(
        "next", help="Lease the next task for an external worker.")
    p_tnext.add_argument("--run", required=True, help="Run id.")
    p_tnext.add_argument("--worker", required=True, help="Agent/worker id.")
    p_tnext.add_argument("--lease-ttl", dest="lease_ttl", default="300",
                         help="Lease TTL in seconds (default 300).")
    p_tnext.set_defaults(func=_cmd_task_next)

    p_tsub = task_sub.add_parser(
        "submit", help="Submit a task result (validated, idempotent).")
    p_tsub.add_argument("task_id", help="Task id from `papers task next`.")
    p_tsub.add_argument("--input", required=True,
                        help="Path to the result JSON payload.")
    p_tsub.add_argument("--run", default=None,
                        help="Run id (or recovered from the task row).")
    p_tsub.add_argument("--worker", default=None,
                        help="Worker id (defaults to the task's lease owner).")
    p_tsub.set_defaults(func=_cmd_task_submit)

    p_elist = evidence_sub.add_parser(
        "list", help="List the run's evidence records.")
    p_elist.add_argument("--run", required=True, help="Run id.")
    p_elist.set_defaults(func=_cmd_evidence_list)

    cite_sub = _ensure_group("citations", "Group: citations (audit)")
    p_caudit = cite_sub.add_parser(
        "audit", help="Run citation + claim-support audit (JSON envelope).")
    p_caudit.add_argument("--run", required=True, help="Run id.")
    p_caudit.set_defaults(func=_cmd_citations_audit)

    # All remaining NOT_IMPLEMENTED commands, declared from the _NOT_IMPL table.
    for cmd, spec in _NOT_IMPL.items():
        if cmd in _implemented_cmds:
            continue
        parts = cmd.split()
        if len(parts) == 1:
            target_sub = sub
            leaf = parts[0]
        else:
            group_name, leaf = parts[0], parts[1]
            if group_name not in _group_map:
                gp = sub.add_parser(group_name, help=f"Group: {group_name} [Phase 1: not implemented]")
                gp_sub = gp.add_subparsers(dest=f"{group_name}_sub", metavar="subcommand")
                _group_map[group_name] = gp_sub
            target_sub = _group_map[group_name]

        p = target_sub.add_parser(
            leaf,
            help=f"[NOT IMPLEMENTED — planned: {spec['phase']}] {spec['desc']}",
        )
        for pos in spec["pos"]:
            p.add_argument(pos, nargs="?", default=None,
                          help=f"[documented] {spec['desc']} (positional '{pos}', unused in Phase 1)")
        for opt in spec["opt"]:
            if opt in FLAGS:
                p.add_argument(opt, action="store_true", default=False,
                               help=f"[documented] flag, unused in Phase 1 (planned: {spec['phase']})")
            else:
                p.add_argument(opt, default=None,
                               help=f"[documented] unused in Phase 1 (planned: {spec['phase']})")
        p.set_defaults(func=lambda a, _cmd=cmd: _not_implemented(_cmd, a))

    return parser


# Track created group subparsers to avoid duplicates on re-entry.
_group_map: dict[str, argparse._SubParsersAction] = {}


def main(argv: Sequence[str] | None = None) -> int:
    global _group_map
    _group_map.clear()
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False) and args.cmd is None:
        _cmd_version(args)
        return 0

    if args.cmd is None:
        # No subcommand: print help, exit 0.
        parser.print_help()
        return 0

    func = getattr(args, "func", None)
    if func is None:
        # A group command with no sub-subcommand (e.g. bare "review").
        env = error(
            "MISSING_SUBCOMMAND",
            message=f"Command '{args.cmd}' requires a subcommand. See: papers {args.cmd} --help",
            retryable=False,
            details={"command": args.cmd},
        )
        emit(env)
        return 2

    try:
        func(args)
        return 0
    except CliError as e:
        env = error(
            e.code,
            message=e.message,
            retryable=e.retryable,
            details=e.details,
        )
        emit(env)
        return e.exit_code
    except KeyboardInterrupt:
        env = error(
            "INTERRUPTED",
            message="Command interrupted by user.",
            retryable=False,
            details={},
        )
        emit(env)
        return 130
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        env = error(
            "INTERNAL_ERROR",
            message=str(e),
            retryable=False,
            details={"exception_type": type(e).__name__},
        )
        emit(env)
        return 1


def cli_entry() -> None:
    """Console script entry point (wired in pyproject.toml)."""
    sys.exit(main())
