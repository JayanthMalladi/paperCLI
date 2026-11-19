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
    # No contract command is a stub anymore (Phases 1-7). The list is
    # kept (empty) so the test stays the guard that a NEW documented
    # command is wired live or returns a NOT_IMPLEMENTED envelope -- a
    # command that exits 0 while still a stub is caught here.
    # Phase 3: "read" + "ingest"; Phase 4: "review plan" / "task
    # next" / "task submit" / "evidence list"; Phase 5: "citations
    # audit"; Phase 6: the review lifecycle; Phase 7: "skill export"
    # + "mcp serve" (all live; covered in their phase test files).
]


def test_no_stub_commands_remain(tmp_project):
    """Every documented contract command is live (Phases 1-7): none of
    them may exit 2 with a NOT_IMPLEMENTED envelope anymore."""
    assert _STUB_COMMANDS == [], _STUB_COMMANDS
    # The last stub (Phase 7 skill export) now exits 0 with an ok
    # envelope (the folder is written; nothing is installed).
    rc, out, _ = run_papers("skill", "export", "--out",
                            "./papercli-skill", cwd=tmp_project)
    assert rc == 0
    env = json.loads(out.strip().splitlines()[-1])
    assert env["status"] == "ok"
    assert "SKILL.md" in env["data"]["files"]


def test_former_stub_commands_exit_2_with_envelope(tmp_project):
    """(Kept for the contract: any command that is STILL a stub must
    exit 2 with a well-formed NOT_IMPLEMENTED envelope, and a bare
    group must exit 2 with MISSING_SUBCOMMAND.)"""
    for args in _STUB_COMMANDS:
        rc, out, _ = run_papers(*args, cwd=tmp_project)
        assert rc == 2, f"{args} exited {rc}, expected 2"
        env = json.loads(out)
        assert env["status"] == "error"
        assert env["error"]["code"] == "NOT_IMPLEMENTED"
        group_commands = {"review", "task", "evidence", "citations",
                          "skill", "mcp"}
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
