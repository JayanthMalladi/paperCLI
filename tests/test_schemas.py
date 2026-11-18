"""Schema validation round-trip tests.

Every published schema must (a) be loadable JSON, (b) accept a valid
instance, and (c) reject an instance missing its required keys.
"""

from __future__ import annotations

import json

from papercli.agent_contract import load_schema, schema_names, validate
from papercli.agent_contract.schemas import SCHEMAS_DIR
from tests.conftest import run_papers


# ---- CLI envelope contract (defect D1) ---------------------------------

def _assert_is_envelope(out: str) -> dict:
    env = json.loads(out)
    assert env["schema_version"] == "1.0"
    assert env["status"] in ("ok", "error")
    assert set(env) == {"schema_version", "status", "data", "warnings"}
    return env


def test_schema_default_output_is_envelope(tmp_project):
    # Spec section 6: default stdout is one JSON envelope.
    rc, out, _ = run_papers("schema", cwd=tmp_project)
    assert rc == 0
    env = _assert_is_envelope(out)
    assert env["status"] == "ok"
    assert "envelope" in env["data"]["schemas"]


def test_schema_json_flag_output_is_envelope(tmp_project):
    rc, out, _ = run_papers("schema", "--json", cwd=tmp_project)
    assert rc == 0
    env = _assert_is_envelope(out)
    assert env["status"] == "ok"
    assert "envelope" in env["data"]["schemas"]



def test_all_schemas_load():
    names = schema_names()
    expected = {"envelope", "brief", "plan", "tasks", "screening",
                "evidence", "claims", "bibliography", "run_summary"}
    assert expected.issubset(set(names))
    for name in names:
        doc = load_schema(name)
        json.dumps(doc)  # must be JSON-serializable
        assert doc.get("$schema"), f"{name} missing $schema"
        assert doc.get("type") in ("object", None), f"{name} unexpected type"


def test_schemas_files_on_disk():
    for name in schema_names():
        p = SCHEMAS_DIR / f"{name}.json"
        assert p.exists()
        assert p.stat().st_size > 0


def _valid_instance(name: str) -> dict:
    """A minimal valid instance per schema (round-trip input)."""
    fixtures = {
        "envelope": {"schema_version": "1.0", "status": "ok", "data": None, "warnings": []},
        "brief": {
            "schema_version": "1.0",
            "question": "Q?",
            "purpose": "purpose",
            "scope": "scope",
            "outputs": ["md"],
            "limits": {
                "wall_minutes": 360, "max_search_queries": 30,
                "max_discovered_records": 1000, "max_full_text_papers": 60,
                "max_model_calls": 300, "max_input_tokens": 1000000,
                "max_output_tokens": 150000, "max_estimated_cost_usd": None,
            },
        },
        "plan": {"schema_version": "1.0", "plan_id": "p1", "question": "Q?",
                  "limits": {
                      "wall_minutes": 360, "max_search_queries": 30,
                      "max_discovered_records": 1000, "max_full_text_papers": 60,
                      "max_model_calls": 300, "max_input_tokens": 1000000,
                      "max_output_tokens": 150000, "max_estimated_cost_usd": None}},
        "tasks": {"task_id": "t1", "type": "search", "state": "pending"},
        "screening": {"paper_id": "p1", "stage": "title_abstract", "decision": "include"},
        "evidence": {"evidence_id": "e1", "paper_id": "p1",
                     "access_level": "full_text", "finding": "f"},
        "claims": {"claim_id": "c1", "text": "a claim", "evidence_ids": ["e1"],
                   "audit_status": "supported"},
        "bibliography": {"references": [{"citation_key": "smith2020"}]},
        "run_summary": {"run_id": "r1", "state": "completed"},
    }
    return fixtures[name]


def test_valid_instances_roundtrip():
    for name in schema_names():
        inst = _valid_instance(name)
        assert validate(inst, name, full=False) == [], (name, inst)


def test_missing_required_fails_structural():
    cases = {
        "envelope": {"status": "ok"},
        "tasks": {"type": "search"},
        "evidence": {"paper_id": "p1", "finding": "f"},
        "run_summary": {},
    }
    for name, inst in cases.items():
        errs = validate(inst, name, full=False)
        assert errs, f"{name}: expected structural failure"
        joined = " ".join(errs)
        schema = load_schema(name)
        missing = [k for k in schema.get("required", []) if k not in inst]
        assert missing, f"{name}: fixture should miss a required key"


def test_default_budget_guardrails_in_brief():
    """The brief template must carry the documented default guardrails."""
    from papercli.config.templates import BRIEF_TEMPLATE

    # wall_minutes 360 etc. are the documented ceilings from spec sec.7.
    assert "wall_minutes: 360" in BRIEF_TEMPLATE
    assert "max_search_queries: 30" in BRIEF_TEMPLATE
    assert "max_discovered_records: 1000" in BRIEF_TEMPLATE
    assert "max_full_text_papers: 60" in BRIEF_TEMPLATE
    assert "max_model_calls: 300" in BRIEF_TEMPLATE
    assert "max_input_tokens: 1000000" in BRIEF_TEMPLATE
    assert "max_output_tokens: 150000" in BRIEF_TEMPLATE
    assert "max_estimated_cost_usd: null" in BRIEF_TEMPLATE


def test_full_validation_uses_jsonschema_when_present():
    import jsonschema  # the optional dep is a hard test requirement here
    from papercli.agent_contract.schemas import _jsonschema_validate

    schema = load_schema("evidence")
    good = _valid_instance("evidence")
    assert _jsonschema_validate(good, schema) == []
    bad = {"evidence_id": "e1", "paper_id": "p1", "access_level": "NOT_A_LEVEL",
           "finding": "f"}
    assert _jsonschema_validate(bad, schema) != []


# ---- schema --validate input errors (defect D3) -------------------------

def test_schema_validate_missing_input_exits_2(tmp_project):
    # A missing --input file is an input error: exit 2, not an internal 1.
    rc, out, _ = run_papers(
        "schema", "--validate", "--input", "no-such-file.json",
        cwd=tmp_project,
    )
    assert rc == 2
    env = json.loads(out)
    assert env["status"] == "error"
    assert env["error"]["code"] == "INPUT_FILE_NOT_FOUND"
    assert "no-such-file.json" in env["error"]["message"]


def test_schema_validate_invalid_json_input_exits_2(tmp_project, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    rc, out, _ = run_papers(
        "schema", "--validate", "--input", str(bad),
        cwd=tmp_project,
    )
    assert rc == 2
    env = json.loads(out)
    assert env["error"]["code"] == "INPUT_INVALID_JSON"


def test_schema_validate_valid_input_still_exits_0(tmp_project, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(
        {"schema_version": "1.0", "status": "ok", "data": None, "warnings": []}
    ), encoding="utf-8")
    rc, out, _ = run_papers(
        "schema", "--validate", "--input", str(good),
        cwd=tmp_project,
    )
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"
    assert env["data"]["valid"] is True

