"""Phase 5 batch 1b: global model rate limiter + budget reservations.

Offline: the limiter's clock/sleep are injectable, so the 45-rpm bucket
and the reserve/reconcile/exhaustion semantics are exercised deterministically
without real waits (spec section 12).
"""

from __future__ import annotations

import pytest

from papercli.models.adapter import ModelAdapter, ModelCallOutcome
from papercli.models.rate_limit import (
    DEFAULT_BUDGETS,
    DEFAULT_RPM,
    ModelBudget,
    RateLimiter,
)
import httpx


def test_default_rate_limit_is_45_rpm():
    assert DEFAULT_RPM == 45
    assert RateLimiter().rpm == 45


def test_rapid_calls_throttle_after_capacity():
    """10 rapid calls against a 5-token bucket: the first 5 pass
    immediately; the 6th+ is throttled (spec's '10 rapid calls -> 6th+
    throttled', scaled down so no real waiting is needed)."""
    lim = RateLimiter(requests_per_minute=60, capacity=5)
    results = [lim.acquire() for _ in range(10)]
    assert sum(1 for r in results[:5] if r.granted) == 5
    assert all(r.throttled for r in results[5:])
    # Throttled calls report a positive, bounded backoff wait.
    for r in results[5:]:
        assert r.wait_seconds > 0
        assert r.wait_seconds <= 120


def test_acquire_reports_wait_without_blocking():
    lim = RateLimiter(requests_per_minute=60, capacity=2)
    lim.acquire()
    lim.acquire()
    r = lim.acquire()
    assert r.throttled and r.wait_seconds > 0


def test_budget_reserve_reconcile_returns_surplus():
    b = ModelBudget({"max_model_calls": 3, "max_input_tokens": 100,
                     "max_output_tokens": 10})
    res = b.reserve(model_calls=1, input_tokens=60, output_tokens=5)
    assert res is not None
    assert b.remaining()["input_tokens"] == 40
    rep = b.reconcile(res, actual_model_calls=1, actual_input_tokens=30,
                      actual_output_tokens=2)
    assert rep["deficit"] is False
    # Free = ceiling - used_total - open_reservations: the 60/5 hold is
    # released and only the actual 30/2 is billed, so 30 input / 3 output
    # of reserved surplus stay free (not spent).
    assert b.remaining()["input_tokens"] == 100 - 30   # hold released, 30 unused
    assert b.remaining()["output_tokens"] == 10 - 2
    assert b.remaining()["model_calls"] == 2
    assert b.actual["input_tokens"] == 30


def test_budget_deficit_counts_against_pool():
    b = ModelBudget({"max_model_calls": 3, "max_input_tokens": 100,
                     "max_output_tokens": 10})
    res = b.reserve(model_calls=1, input_tokens=50, output_tokens=5)
    rep = b.reconcile(res, actual_model_calls=1, actual_input_tokens=90,
                      actual_output_tokens=9)
    assert rep["deficit"] is True
    # Free = ceiling - used_total - open_reservations. The hold (50/5) is
    # released and the FULL actual usage (90/9) is billed; the 40/4 deficit
    # over the reservation is disclosed, not silently absorbed.
    assert b.remaining()["input_tokens"] == 100 - 90
    assert b.remaining()["output_tokens"] == 10 - 9


def test_budget_exhaustion_reserves_partial_synthesis():
    """On exhaustion, stop new dispatches BUT keep a slice for a partial
    synthesis + status bundle (spec section 12)."""
    b = ModelBudget({"max_model_calls": 2, "max_input_tokens": 1000,
                     "max_output_tokens": 1000})
    r1 = b.reserve(model_calls=1)
    b.reconcile(r1, actual_model_calls=1, actual_input_tokens=0,
                actual_output_tokens=0)
    r2 = b.reserve(model_calls=1)
    b.reconcile(r2, actual_model_calls=1, actual_input_tokens=0,
                actual_output_tokens=0)
    assert b.exhausted
    assert b.reserve(model_calls=1) is None
    assert b.allow_dispatch() is False
    # The partial-synthesis slice must still be dispatchable.
    assert b.allow_dispatch(purpose="partial_synthesis") is True
    sres = b.reserve(purpose="partial_synthesis")
    assert sres is not None
    assert b.partial_synthesis_reserved
    d = b.to_dict()
    assert d["exhausted"] is True and d["partial_synthesis_reserved"] is True


def test_unknown_price_never_free_in_adapter(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]})))
    b = ModelBudget(DEFAULT_BUDGETS)
    adapter = ModelAdapter(base_url="https://fake.local", model="unknown-model",
                           api_key="k", client=client, budget=b,
                           on_record=lambda r: None)
    res = adapter.complete_json(schema={"type": "object"}, system="s",
                                user="u", task_type="t")
    assert res["cost"]["pricing_known"] is False
    assert res["cost"]["estimated_cost_usd"] is None


def test_adapter_budget_exhaustion_stops_dispatch(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}})))
    b = ModelBudget({"max_model_calls": 1})
    adapter = ModelAdapter(base_url="https://fake.local", model="m",
                           api_key="k", client=client, budget=b,
                           on_record=lambda r: None)
    first = adapter.complete_json(schema={"type": "object"}, system="s",
                                  user="u", task_type="t")
    assert first["outcome"] == ModelCallOutcome.SUCCESS
    second = adapter.complete_json(schema={"type": "object"}, system="s",
                                   user="u", task_type="t")
    assert second["outcome"] == ModelCallOutcome.SKIPPED
    assert second.get("budget_exhausted") is True


def test_adapter_throttled_becomes_retry_wait():
    """Global limiter fully drained -> the call records retry_wait, no HTTP."""
    import httpx
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    lim = RateLimiter(requests_per_minute=60, capacity=0)  # no tokens, ever
    b = ModelBudget()
    adapter = ModelAdapter(base_url="https://fake.local", model="m",
                           api_key="k", client=client, rate_limiter=lim,
                           budget=b, on_record=lambda r: None)
    res = adapter.complete_json(schema={"type": "object"}, system="s",
                                user="u", task_type="t")
    assert res["outcome"] == ModelCallOutcome.RETRY_WAIT
    assert res["throttled"] is True
    assert not calls


# ---------------------------------------------------------------------------
# One LIVE smoke (opt-in via the @network marker): a tiny real call to the
# configured endpoint (max_tokens ~50), asserting the adapter round-trips and
# records a Model-call row. Skipped gracefully when the key env var is
# absent -- the offline suite never touches the network.
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_live_model_smoke_records_call_row(tmp_path):
    """ONE live smoke: a tiny real call (max_tokens ~50) to the configured
    endpoint, asserting the adapter round-trips AND records a Model-call
    row. Skips gracefully when the endpoint + key are not configured.

    The endpoint + key come from the documented .env vars
    (PAPERS_LLM_BASE_URL / PAPERS_LLM_API_KEY, spec section 13), falling
    back to the config-override names. No secret value is printed.
    """
    import os
    from papercli.config.loader import Config
    from papercli.storage.db import PaperStore

    base_url = (os.environ.get("PAPERS_LLM_BASE_URL")
                or os.environ.get("PAPERS_MODEL_BASE_URL") or "").strip()
    model = (os.environ.get("PAPERS_LLM_MODEL")
             or os.environ.get("PAPERS_MODEL_ID") or "claude-sonnet-5-5").strip()
    key_ref = "PAPERS_LLM_API_KEY"
    key = (os.environ.get(key_ref) or "").strip()
    if not base_url or not key:
        pytest.skip("model endpoint/key not configured "
                    "(PAPERS_LLM_BASE_URL / PAPERS_LLM_API_KEY absent)")

    cfg = Config({
        "model": {"adapter": "openai_compatible", "base_url": base_url,
                  "model": model, "api_key_env": key_ref},
        "execution": {"request_timeout_seconds": 30},
    }, sources=["test"], env={key_ref: key})

    store = PaperStore(tmp_path / "smoke.db")
    rid = store.create_run()
    adapter = ModelAdapter.from_config(cfg,
                                       on_record=lambda rec: store.record_model_call(
                                           rid, outcome=rec["outcome"],
                                           model=rec.get("model"),
                                           task_type=rec.get("task_type"),
                                           prompt_hash=rec.get("prompt_hash"),
                                           prompt_version=rec.get("prompt_version"),
                                           input_tokens=rec.get("input_tokens"),
                                           output_tokens=rec.get("output_tokens"),
                                           usage=rec.get("cost") or {},
                                           endpoint=rec.get("endpoint"),
                                           api_key_env=rec.get("api_key_env")))
    res = adapter.complete_text(system="Reply with the single word: ping.",
                                user="ping", task_type="smoke", run_id=rid,
                                max_tokens=50)
    adapter.close()
    assert res["outcome"] == ModelCallOutcome.SUCCESS, res.get("validation_errors")
    rows = store.model_calls_for_run(rid)
    assert rows and rows[0]["outcome"] == "success"
    # The selected endpoint is visible (nonsecret) in the recorded row; the
    # secret value is NOT present anywhere in the row.
    assert rows[0]["endpoint"] == base_url
    assert key not in str(rows[0])
    store.close()
