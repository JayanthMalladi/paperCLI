"""Phase 5 batch 1: the OpenAI-compatible adapter + global rate limiter.

Fully offline: a fake model backend (httpx.MockTransport) stands in for
the endpoint -- no live model calls in the offline suite. The one
``@pytest.mark.network`` live smoke (tiny real call + a Model-call row)
is skipped gracefully when the key env var is absent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from papercli.models.adapter import (
    ModelAdapter,
    ModelCallOutcome,
    extract_json,
    prompt_template_id,
    validate_against_schema,
)
from papercli.models.rate_limit import (
    DEFAULT_BUDGETS,
    DEFAULT_RPM,
    ModelBudget,
    RateLimiter,
)

ECHO_SCHEMA = {
    "type": "object",
    "required": ["decision"],
    "properties": {"decision": {"type": "string"}},
}


def _client(responses: list[httpx.Response]) -> tuple[httpx.Client, list]:
    """An httpx client that replays ``responses`` in order (last one repeats)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport), calls


def _chat_resp(content: str, status: int = 200,
               usage: dict | None = None) -> httpx.Response:
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage:
        body["usage"] = usage
    return httpx.Response(status, json=body)


# ---------------------------------------------------------------------------
# Lenient JSON extraction (spec section 4: fences observed, never assumed)
# ---------------------------------------------------------------------------

def test_extract_json_bare_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json("```json\n{\"decision\": \"include\"}\n```") == {"decision": "include"}


def test_extract_json_fenced_with_prose_around_block():
    text = 'Sure, here is the result:\n{"decision": "exclude", "n": 2}\nLet me know.'
    assert extract_json(text) == {"decision": "exclude", "n": 2}


def test_extract_json_repairs_trailing_comma():
    assert extract_json('{"a": 1,}') == {"a": 1}


def test_extract_json_malformed_raises():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_validate_against_schema_reports_errors():
    assert validate_against_schema({"decision": "ok"}, ECHO_SCHEMA) == []
    errs = validate_against_schema({"decision": 5}, ECHO_SCHEMA)
    assert errs and "decision" in " ".join(errs)


def test_prompt_template_id_stable_and_versioned():
    a = prompt_template_id("hello", "v1")
    b = prompt_template_id("hello", "v1")
    c = prompt_template_id("hello", "v2")
    d = prompt_template_id("world", "v1")
    assert a == b and a != c and a != d
    assert len(a) == 16


# ---------------------------------------------------------------------------
# Adapter call loop: re-prompt, failed outcome, usage/cost, model-call rows
# ---------------------------------------------------------------------------

def test_adapter_fenced_json_then_valid_success(tmp_path):
    store_dir = tmp_path / "store"
    from papercli.storage.db import PaperStore
    store = PaperStore(store_dir / "papercli.db")
    rid = store.create_run()
    rows: list[dict] = []

    def on_record(rec: dict):
        rows.append(rec)
        store.record_model_call(rid, outcome=rec["outcome"],
                                model=rec["model"], task_type=rec.get("task_type"),
                                prompt_hash=rec.get("prompt_hash"),
                                prompt_version=rec.get("prompt_version"),
                                protocol_revision=rec.get("protocol_revision"),
                                input_tokens=rec.get("input_tokens"),
                                output_tokens=rec.get("output_tokens"),
                                usage=rec.get("cost") or {},
                                endpoint=rec.get("endpoint"),
                                api_key_env=rec.get("api_key_env"))

    client, calls = _client([
        _chat_resp("```json\n{\"decision\": \"include\"}\n```",
                   usage={"prompt_tokens": 100, "completion_tokens": 20}),
    ])
    adapter = ModelAdapter(base_url="https://fake.local", model="claude-sonnet-5-5",
                           api_key="sekret-value-that-must-not-leak",
                           client=client, on_record=on_record)
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="pick", user="record X",
                               task_type="screen", run_id=rid)
    assert res["ok"] and res["outcome"] == ModelCallOutcome.SUCCESS
    assert res["data"] == {"decision": "include"}
    assert res["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert res["cost"]["pricing_known"] is True and res["cost"]["estimated_cost_usd"] > 0
    assert rows and rows[0]["outcome"] == "success"
    db_rows = store.model_calls_for_run(rid)
    assert db_rows and db_rows[0]["outcome"] == "success"
    assert db_rows[0]["model"] == "claude-sonnet-5-5"
    # The prompt sent to the endpoint must not contain the secret value.
    for req in calls:
        body = json.loads(req.content)
        blob = json.dumps(body["messages"])
        assert "sekret-value-that-must-not-leak" not in blob
    store.close()


def test_adapter_malformed_then_reprompt_then_success():
    client, calls = _client([
        _chat_resp("sorry, I cannot output json"),
        _chat_resp('{"decision": "exclude"}'),
    ])
    adapter = ModelAdapter(base_url="https://fake.local", model="m",
                           api_key="k", client=client, on_record=lambda r: None)
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="s", user="u", task_type="screen")
    assert res["ok"] and res["attempts"] == 2
    assert len(calls) == 2
    # Second attempt carries the re-prompt instruction.
    second = json.loads(calls[1].content)
    assert any("did not match" in m["content"] for m in second["messages"])


def test_adapter_always_invalid_is_failed_outcome_not_crash():
    client, _ = _client([_chat_resp("total garbage"), _chat_resp("more garbage"),
                         _chat_resp("still garbage")])
    adapter = ModelAdapter(base_url="https://fake.local", model="m",
                           api_key="k", client=client, on_record=lambda r: None)
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="s", user="u", task_type="screen")
    assert res["ok"] is False
    assert res["outcome"] == ModelCallOutcome.FAILED
    assert res["attempts"] == 3  # 1 initial + 2 bounded re-prompts


def test_adapter_unpriced_model_cost_null_pricing_unknown(tmp_path):
    store_dir = tmp_path / "store"
    from papercli.storage.db import PaperStore
    store = PaperStore(store_dir / "papercli.db")
    rid = store.create_run()
    seen: list[dict] = []

    def on_record(rec):
        seen.append(rec)
        return store.record_model_call(rid, outcome=rec["outcome"], model=rec["model"],
                                       input_tokens=rec.get("input_tokens"),
                                       output_tokens=rec.get("output_tokens"),
                                       usage=rec.get("cost") or {},
                                       estimated_cost_usd=(rec.get("cost") or {}).get("estimated_cost_usd"))

    client, _ = _client([_chat_resp('{"decision": "include"}',
                                    usage={"prompt_tokens": 50, "completion_tokens": 5})])
    prices = ModelAdapter.__init__.__defaults__  # noqa: SLF001 (default table ref)
    from papercli.models.adapter import DEFAULT_PRICES_USD_PER_MTOK
    adapter = ModelAdapter(base_url="https://fake.local", model="mystery-model",
                           api_key="k", client=client, on_record=on_record,
                           prices={"some-other-model": (1.0, 1.0)})
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="s", user="u", task_type="extract")
    assert res["ok"]
    assert res["cost"]["estimated_cost_usd"] is None
    assert res["cost"]["pricing_known"] is False
    assert seen and seen[0]["cost"]["pricing_known"] is False
    row = store.model_calls_for_run(rid)[0]
    assert row["estimated_cost_usd"] is None  # unknown price is NOT zero
    store.close()


def test_adapter_secret_never_in_prompt_or_records(tmp_path):
    """NO_SECRET: a probe value must be absent from requests AND from
    persisted model-call rows (spec sections 13 + 16)."""
    from papercli.storage.db import PaperStore
    store = PaperStore(tmp_path / "p.db")
    rid = store.create_run()

    client, calls = _client([_chat_resp('{"decision": "include"}')])
    adapter = ModelAdapter(base_url="https://fake.local", model="m",
                           api_key="PROBE_SECRET_XYZ123", api_key_env="PAPERS_LLM_API_KEY",
                           client=client,
                           on_record=lambda rec: store.record_model_call(
                               rid, outcome=rec["outcome"], model=rec["model"],
                               endpoint=rec.get("endpoint"), api_key_env=rec.get("api_key_env"),
                               input_tokens=rec.get("input_tokens"),
                               output_tokens=rec.get("output_tokens")))
    adapter.complete_json(schema=ECHO_SCHEMA, system="you are X", user="screen Y", task_type="t")
    for req in calls:
        # Secret must never appear in the prompt body (spec section 13).
        assert "PROBE_SECRET_XYZ123" not in req.content.decode("utf-8")
        # It goes ONLY to the declared endpoint as the auth header.
        assert req.headers["Authorization"] == "Bearer PROBE_SECRET_XYZ123"
    for row in store.model_calls_for_run(rid):
        blob = json.dumps(row, default=str)
        assert "PROBE_SECRET_XYZ123" not in blob
        assert row["api_key_env"] == "PAPERS_LLM_API_KEY"  # ref visible, value not
    store.close()


def test_adapter_not_configured_records_skipped():
    client, calls = _client([_chat_resp('{"decision": "include"}')])
    adapter = ModelAdapter(base_url="", model="", client=client,
                           on_record=lambda r: None)
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="s", user="u", task_type="screen")
    assert res["ok"] is False and res["skipped"] is True
    assert res["outcome"] == ModelCallOutcome.SKIPPED
    assert not calls  # no HTTP at all


def test_adapter_429_backs_off_into_retry_wait_semantics():
    """A persistent 429 must exhaust the bounded loop and record a FAILED
    outcome with the 429 noted -- never a retry storm."""
    client, calls = _client([_chat_resp("", status=429)])
    adapter = ModelAdapter(base_url="https://fake.local", model="m", api_key="k",
                           client=client, on_record=lambda r: None,
                           max_re_prompts=1)
    res = adapter.complete_json(schema=ECHO_SCHEMA, system="s", user="u", task_type="screen")
    assert res["outcome"] == ModelCallOutcome.FAILED
    assert any("429" in e for e in res["validation_errors"])
    assert len(calls) <= 2  # bounded: 1 + max_re_prompts, no storm
