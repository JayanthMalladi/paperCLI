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

    "review plan": {"phase": "Phase 4", "desc": "Generate a validated research plan from a brief",
                    "pos": [], "opt": ["--brief", "--out"]},
    "review run": {"phase": "Phase 4", "desc": "Launch a review run (foreground or detached)",
                   "pos": [], "opt": ["--plan", "--detach"]},
    "review status": {"phase": "Phase 4", "desc": "Query run status",
                      "pos": ["run_id"], "opt": []},
    "review logs": {"phase": "Phase 4", "desc": "Stream run logs",
                    "pos": ["run_id"], "opt": ["--follow"]},
    "review resume": {"phase": "Phase 4", "desc": "Resume an interrupted run",
                      "pos": ["run_id"], "opt": ["--detach"]},
    "review cancel": {"phase": "Phase 4", "desc": "Cancel a running review",
                      "pos": ["run_id"], "opt": []},
    "review export": {"phase": "Phase 7", "desc": "Export the output bundle for a completed run",
                      "pos": ["run_id"], "opt": ["--formats"]},
    "task next": {"phase": "Phase 4", "desc": "Lease the next task for an external worker",
                  "pos": [], "opt": ["--run", "--worker"]},
    "task submit": {"phase": "Phase 4", "desc": "Submit a task result",
                    "pos": ["task_id"], "opt": ["--input"]},
    "evidence list": {"phase": "Phase 4", "desc": "List evidence records for a run",
                      "pos": [], "opt": ["--run"]},
    "citations audit": {"phase": "Phase 7", "desc": "Run citation and claim-support audit",
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
        emit(ok(
            data={
                "run_id": run_id,
                "document_id": report["document_id"],
                "paper_id": report.get("paper_id") or paper_id,
                "kind": report["kind"],
                "content_hash": report["content_hash"],
                "access_status": report["access_status"],
                "page_count": report["page_count"],
                "sections_stored": report["sections_stored"],
                "scanned": report["scanned"],
                "identity_status": report["identity_status"],
                "identity": report.get("identity"),
            },
            warnings=warnings,
        ))


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

    # All remaining NOT_IMPLEMENTED commands, declared from the _NOT_IMPL table.
    for cmd, spec in _NOT_IMPL.items():
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
