"""Phase 7: the stdio MCP server (spec section 15: "optional MCP wraps
the same core functions").

The server speaks MCP JSON-RPC over stdio: initialize -> tools/list ->
tools/call. Every tool is a thin wrapper over the SAME CLI handler the
console command uses, so the tool surface cannot drift from the CLI
contract. Tools are exercised by SPAWNING the server as a stdio child
(the agent-session connection mode) with the test interpreter.

No network in any test: the offline paths are used (doctor --skip-
network, schema --list, and a search that fails provider VALIDATION --
an unknown provider is a config error, not a network call).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from papercli.agent_contract.mcp_server import (
    TOOL_NAMES,
    handle_message,
    invoke_cli,
    tool_schemas,
)
from papercli.agent_contract.schemas import validate_envelope

#: The tool surface, in order (mirrors the CLI command set).
EXPECTED_TOOLS = [
    "search", "resolve", "fetch", "read", "ingest",
    "review_plan", "review_run", "review_status", "review_logs",
    "review_resume", "review_cancel", "review_export",
    "task_next", "task_submit", "evidence_list", "citations_audit",
    "doctor", "schema",
]

_SRC = Path(__file__).resolve().parents[1] / "src"


def _server_argv() -> list[str]:
    """Spawn the MCP server the way an agent session would: a stdio
    child speaking JSON-RPC over stdin/stdout, with the package
    importable (the src path is added in the shim, mirroring
    tests/conftest.py)."""
    shim = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "from papercli.agent_contract.mcp_server import serve\n"
        "sys.exit(serve())\n"
    )
    return [sys.executable, "-c", shim]


def _talk(messages: list[dict], timeout: int = 90,
          cwd: Path | None = None) -> list[dict]:
    """Send JSON-RPC messages over stdio; return the response objects
    (notifications produce no response)."""
    proc = subprocess.Popen(
        _server_argv(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        cwd=str(cwd) if cwd is not None else None,
    )
    data = "\n".join(json.dumps(m) for m in messages) + "\n"
    out, err = proc.communicate(input=data, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"server exited {proc.returncode}: "
                             f"{err[-500:]}")
    return [json.loads(l) for l in out.strip().splitlines() if l.strip()]


def _result_envelope(response: dict) -> dict:
    """The CLI envelope carried in a tools/call result."""
    assert "result" in response, f"no result: {response}"
    assert "content" in response["result"]
    texts = response["result"]["content"]
    assert texts and texts[0]["type"] == "text"
    return json.loads(texts[0]["text"])


# ---------------------------------------------------------------------------
# Handshake: initialize -> tools/list (the agent-session connection)
# ---------------------------------------------------------------------------

def test_mcp_handshake_initialize_and_tools_list(tmp_path):
    resp = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ], cwd=tmp_path)
    by_id = {r.get("id"): r for r in resp}
    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2024-11-05"
    assert "tools" in init["capabilities"]
    assert init["serverInfo"]["name"] == "papercli-mcp"
    tools = by_id[2]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == EXPECTED_TOOLS
    # Each tool carries a JSON-schema input block (the flags the CLI
    # command accepts) -- no second schema that could drift.
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
        assert isinstance(t["inputSchema"]["required"], list)


def test_mcp_tool_surface_is_1_to_1_with_the_cli_commands():
    """The tool surface maps 1:1 onto the CLI commands (the drift guard):
    every tool exists, in the same order, and each has a CLI command
    behind it in the live parser (a tool WITHOUT a CLI command could
    drift from the contract)."""
    from papercli.cli.main import build_parser

    schemas = tool_schemas()
    assert [s["name"] for s in schemas] == EXPECTED_TOOLS
    assert TOOL_NAMES == EXPECTED_TOOLS
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    for tool in EXPECTED_TOOLS:
        # Top-level CLI commands; group tools map to their group leaf.
        top = tool.split("_")[0]
        def _sub(ap):
            return ap._subparsers._group_actions[0].choices

        if top in ("review", "task", "evidence", "citations"):
            sub_name = tool.split("_", 1)[1]
            assert sub_name in _sub(choices[top]), (tool, sub_name)
        else:
            assert top in choices, tool


# ---------------------------------------------------------------------------
# tools/call: offline paths return structured envelopes, never a crash
# ---------------------------------------------------------------------------

def test_tools_call_doctor_and_schema_offline(tmp_path):
    resp = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "doctor", "arguments": {"skip_network": True}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "schema", "arguments": {"list": True}}},
    ], cwd=tmp_path)
    by_id = {r.get("id"): r for r in resp}
    doctor = _result_envelope(by_id[2])
    assert doctor["status"] == "ok", doctor
    assert validate_envelope(doctor) == []
    assert doctor["data"]["summary"]["total"] > 0
    schema_env = _result_envelope(by_id[3])
    assert schema_env["status"] == "ok"
    assert validate_envelope(schema_env) == []
    # schema --list returns {name: {title, $id, required, name}}.
    names = set(schema_env["data"]["schemas"])
    assert {"envelope", "brief", "plan", "evidence", "claims",
            "bibliography"} <= names
    # Successful calls are not flagged as errors.
    assert by_id[2]["result"]["isError"] is False
    assert by_id[3]["result"]["isError"] is False


def test_tools_call_search_offline_validation_path(tmp_path):
    """A search that fails provider VALIDATION (unknown provider) is
    the offline path: a structured CONFIG_REQUIRED envelope, no
    network, no crash."""
    resp = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search",
                    "arguments": {"query": "fixture query",
                                   "providers": "not-a-provider"}}},
    ], cwd=tmp_path)
    env = _result_envelope([r for r in resp if r.get("id") == 2][0])
    assert env["status"] == "error"
    assert validate_envelope(env) == []
    assert env["error"]["code"] == "CONFIG_REQUIRED"
    assert [r for r in resp if r.get("id") == 2][0]["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Unknown tool / unknown method: structured JSON-RPC, never a crash
# ---------------------------------------------------------------------------

def test_tools_call_unknown_tool_returns_envelope_error(tmp_path):
    resp = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "definitely_not_a_tool", "arguments": {}}},
    ], cwd=tmp_path)
    r2 = [r for r in resp if r.get("id") == 2][0]
    env = _result_envelope(r2)
    assert env["status"] == "error"
    assert env["error"]["code"] == "UNKNOWN_TOOL"
    assert r2["result"]["isError"] is True


def test_unknown_jsonrpc_method_is_an_error_not_a_crash(tmp_path):
    resp = _talk([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
    ], cwd=tmp_path)
    r9 = [r for r in resp if r.get("id") == 9][0]
    assert r9["error"]["code"] == -32601
    assert "Method not found" in r9["error"]["message"]


def test_malformed_line_is_a_parse_error_not_a_crash(tmp_path):
    proc = subprocess.Popen(_server_argv(), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(tmp_path))
    out, _ = proc.communicate(input="not json at all\n"
                                + json.dumps({"jsonrpc": "2.0", "id": 1,
                                              "method": "ping"}) + "\n",
                              timeout=60)
    lines = [json.loads(l) for l in out.strip().splitlines() if l.strip()]
    parse_err = [l for l in lines if l.get("error", {}).get("code") == -32700]
    assert parse_err, lines
    assert lines[-1]["id"] == 1 and lines[-1]["result"] == {}


# ---------------------------------------------------------------------------
# The in-process seam (unit-level, no subprocess): invoke_cli returns
# the CLI's envelope + exit code
# ---------------------------------------------------------------------------

def test_invoke_cli_doctor_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, envelope = invoke_cli(["doctor", "--skip-network"])
    assert code == 0
    assert envelope["status"] == "ok"
    assert validate_envelope(envelope) == []


def test_invoke_cli_unknown_command_is_structured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, envelope = invoke_cli(["definitely-not-a-command"])
    # argparse rejects unknown subcommands with a HUMAN-READABLE usage
    # error on stderr + exit 2 (the documented exception to the one-
    # envelope contract; usage errors never produce a stdout envelope).
    # The seam surfaces that as (2, None); the MCP tools/call layer
    # turns a None envelope into the structured NO_ENVELOPE body.
    assert code == 2
    assert envelope is None


def test_handle_message_initialize_versions():
    m = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26"}}
    r = handle_message(m)
    assert r["result"]["protocolVersion"] == "2025-03-26"
    # A version the server does not advertise falls back to the newest
    # supported one.
    m2 = {"jsonrpc": "2.0", "id": 2, "method": "initialize",
          "params": {"protocolVersion": "9999-01-01"}}
    assert handle_message(m2)["result"]["protocolVersion"] == "2025-03-26"
    # Notifications never produce a response.
    assert handle_message({"jsonrpc": "2.0", "method": "initialized"}) is None
