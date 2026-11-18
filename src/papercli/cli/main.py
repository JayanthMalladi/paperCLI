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
    EXIT_CODES,
    InternalError,
    NotImplemented as NotImplementedErr,
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
    "search": {"phase": "Phase 2", "desc": "Live provider search (Unpaywall, Crossref, OpenAlex, arXiv)",
               "pos": ["query"], "opt": ["--providers", "--limit"]},
    "resolve": {"phase": "Phase 2", "desc": "DOI / arXiv identifier resolution",
                "pos": ["identifier"], "opt": []},
    "fetch": {"phase": "Phase 3", "desc": "Full-text retrieval and local cache",
              "pos": ["identifier"], "opt": ["--out"]},
    "read": {"phase": "Phase 3", "desc": "Page/section extraction from a document",
             "pos": ["file"], "opt": ["--pages", "--max-chars"]},
    "ingest": {"phase": "Phase 3", "desc": "Import a local document into a run",
               "pos": ["file"], "opt": ["--run"]},
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
            with open(args.input, encoding="utf-8") as f:
                data = _json.load(f)
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

    # Default: print all schemas as JSON.
    out: dict[str, dict] = {}
    for name in schema_names():
        out[name] = load_schema(name)
    emit(out if not args.json else {"schemas": out})


def _validate_generic(data: Any, schema_name: str) -> list[str]:
    from papercli.agent_contract.schemas import validate as _validate
    return _validate(data, schema_name, full=True)


def _cmd_version(args: argparse.Namespace) -> None:
    """Human-readable exception: prints version text, not a JSON envelope."""
    print(f"papers {__version__} (agent-papercli)")


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
                          help="Wrap printed schemas in an envelope.")
    p_schema.set_defaults(func=_cmd_schema)

    # version (explicit subcommand, in addition to --version flag)
    p_ver = sub.add_parser("version", help="Print version (human-readable exception).")
    p_ver.set_defaults(func=_cmd_version)

    # All NOT_IMPLEMENTED commands, declared from the _NOT_IMPL table.
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
