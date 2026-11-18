"""CLI exit-code and NOT_IMPLEMENTED envelope tests.

Covers every command in the contract table: implemented commands exit 0,
stubs exit 2 with a well-formed NOT_IMPLEMENTED envelope, and the
documented exit-code mapping is asserted.
"""

from __future__ import annotations

import json

from papercli.cli.errors import EXIT_CODES
from tests.conftest import run_papers


# ---- implemented commands ------------------------------------------------

def test_version_exits_0(tmp_project):
    rc, out, _ = run_papers("version", cwd=tmp_project)
    assert rc == 0
    assert "papers 0.1.0" in out


def test_init_exits_0(tmp_path):
    rc, out, _ = run_papers("init", cwd=tmp_path)
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"


def test_schema_list_exits_0(tmp_project):
    rc, out, _ = run_papers("schema", "--list", cwd=tmp_project)
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"
    assert "envelope" in env["data"]["schemas"]


def test_doctor_exits_0_fresh(tmp_project):
    # A fresh, unconfigured project: doctor reports warnings but exits 0.
    rc, out, _ = run_papers("doctor", "--json", "--skip-network", cwd=tmp_project)
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"
    assert env["data"]["summary"]["healthy"] is True


def test_config_show_exits_0(tmp_project):
    rc, out, _ = run_papers("config", "show", "--redact", cwd=tmp_project)
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"


# ---- NOT_IMPLEMENTED stubs (exit 2) -------------------------------------

#: Phase 1 stubs that still exit 2 with NOT_IMPLEMENTED. Phase 2 wired
#: search/resolve/fetch into live provider commands, so they no longer
#: belong here (their offline behavior is covered in the provider tests).
_STUB_COMMANDS = [
    # Phase 3 implemented "read" and "ingest" (see test_phase3_*.py);
    # the remaining contract commands are still documented NOT_IMPLEMENTED.
    ["review", "plan", "--brief", "b.md", "--out", "plan.yaml"],
    ["review", "run", "--plan", "p.yaml", "--detach"],
    ["review", "status", "RUN_ID"],
    ["review", "logs", "RUN_ID", "--follow"],
    ["review", "resume", "RUN_ID", "--detach"],
    ["review", "cancel", "RUN_ID"],
    ["review", "export", "RUN_ID", "--formats", "md,bib"],
    ["task", "next", "--run", "RUN_ID", "--worker", "AGENT_ID"],
    ["task", "submit", "TASK_ID", "--input", "result.json"],
    ["evidence", "list", "--run", "RUN_ID"],
    ["citations", "audit", "--run", "RUN_ID"],
    ["skill", "export", "--out", "./papercli-skill"],
]


def test_stub_commands_exit_2_with_envelope(tmp_project):
    for args in _STUB_COMMANDS:
        rc, out, _ = run_papers(*args, cwd=tmp_project)
        assert rc == 2, f"{args} exited {rc}, expected 2"
        env = json.loads(out)
        assert env["status"] == "error"
        assert env["error"]["code"] == "NOT_IMPLEMENTED"
        # command path = first token, or first two tokens for a group command
        group_commands = {"review", "task", "evidence", "citations", "skill"}
        expected_cmd = " ".join(args[:2]) if args[0] in group_commands else args[0]
        assert env["error"]["details"]["command"] == expected_cmd
        assert env["schema_version"] == "1.0"
        assert env["warnings"] == []


def test_bare_group_command_exit_2(tmp_project):
    # "review" with no subcommand -> MISSING_SUBCOMMAND, exit 2.
    rc, out, _ = run_papers("review", cwd=tmp_project)
    assert rc == 2
    env = json.loads(out)
    assert env["error"]["code"] == "MISSING_SUBCOMMAND"


def test_no_subcommand_prints_help_exits_0(tmp_project):
    rc, out, err = run_papers(cwd=tmp_project)
    assert rc == 0
    # Help goes to stdout or stderr, not a JSON envelope.
    combined = out + err
    assert "papers" in combined


def test_exit_code_table_is_complete():
    for code in (0, 1, 2, 3, 4, 5, 6, 7, 130):
        assert code in EXIT_CODES, f"exit code {code} missing from table"
