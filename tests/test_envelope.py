"""JSON envelope shape tests.

The envelope is the machine contract: one JSON object on stdout with
schema_version, status, data, warnings. Error envelopes add an ``error``
block. These tests pin the shape that agents rely on.
"""

from __future__ import annotations

import json

import pytest

from papercli.agent_contract import SCHEMA_VERSION, load_schema, validate_envelope
from papercli.cli.envelope import error, ok, parse_line, render


def test_ok_envelope_shape():
    env = ok(data={"run_id": "r1", "state": "running"}, warnings=["w1"])
    assert env["schema_version"] == SCHEMA_VERSION
    assert env["status"] == "ok"
    assert env["data"] == {"run_id": "r1", "state": "running"}
    assert env["warnings"] == ["w1"]
    assert "error" not in env


def test_error_envelope_shape():
    env = error("CONFIG_REQUIRED", "msg", retryable=False,
                 details={"field": "contact.email"}, warnings=["w"])
    assert env["status"] == "error"
    assert env["data"] is None
    assert env["error"]["code"] == "CONFIG_REQUIRED"
    assert env["error"]["message"] == "msg"
    assert env["error"]["retryable"] is False
    assert env["error"]["details"] == {"field": "contact.email"}
    assert env["warnings"] == ["w"]


def test_render_is_single_line():
    line = render(ok(data={"a": [1, 2]}))
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["status"] == "ok"


def test_parse_line_roundtrip():
    line = render(error("X", "y"))
    parsed = parse_line(line)
    assert parsed["status"] == "error"
    assert parsed["error"]["code"] == "X"


def test_parse_line_rejects_bad():
    with pytest.raises(ValueError):
        parse_line("")
    with pytest.raises(ValueError):
        parse_line("not json")
    with pytest.raises(ValueError):
        parse_line(json.dumps({"status": "weird"}))


def test_envelope_matches_published_schema():
    schema = load_schema("envelope")
    good = ok(data={"x": 1})
    assert validate_envelope(good) == []
    # A status outside the enum must fail the published schema.
    bad = {"schema_version": SCHEMA_VERSION, "status": "mystery", "data": None, "warnings": []}
    assert validate_envelope(bad) != []


def test_envelope_schema_forbids_extra_top_level_keys():
    # The published envelope schema is closed (additionalProperties: false).
    schema = load_schema("envelope")
    assert schema["additionalProperties"] is False
    extra = {"schema_version": SCHEMA_VERSION, "status": "ok", "data": None,
             "warnings": [], "rogue": 1}
    assert validate_envelope(extra) != []
