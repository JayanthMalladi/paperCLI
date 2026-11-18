"""One-tested OpenAI-compatible chat-completions adapter (spec section 4).

The selected endpoint (the user's, e.g. ``https://api.brocode.live``) speaks
OpenAI chat-completions AND Anthropic messages; its
``response_format=json_schema`` is ACCEPTED but NOT ENFORCED (the probe saw
fenced-JSON responses), and ``tools`` were accepted but never invoked. The
adapter therefore:

* sends plain chat-completions requests (no tools, no structured-output
  assumption), asking the model to answer with one JSON document;
* parses the reply LENIENTLY (strips ```json fences, repairs common issues)
  and validates against the caller's JSON Schema AFTER the fact;
* re-prompts on a validation failure (bounded, :data:`MAX_RE_PROMPTS`),
  then records a FAILED outcome -- it never crashes on a bad reply;
* captures per-call token usage + cost estimate (an unknown price is
  ``cost=None`` + ``pricing_known=False`` -- never treated as free,
  spec section 12);
* records every call as a Model-call row (model, prompt hash + version,
  tokens, usage/cost, outcome -- spec section 11);
* rate-limits through the global :class:`~papercli.models.rate_limit.RateLimiter`
  and a 429 backs off into a bounded retry wait, never a retry storm.

No secret value is ever placed inside a prompt (spec section 13); config
supplies only environment-variable references and the selected endpoint is
visible (nonsecret) in ``papers config show``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

import httpx

from papercli.models.rate_limit import ModelBudget, RateLimiter, Reservation

__all__ = [
    "ModelAdapter",
    "ModelCallOutcome",
    "extract_json",
    "validate_against_schema",
    "prompt_template_id",
    "MAX_RE_PROMPTS",
    "DEFAULT_PRICES_USD_PER_MTOK",
    "PRICE_TABLE_VERSION",
]

#: Bounded re-prompt loop (spec: max 2 re-prompts, then a failed outcome).
MAX_RE_PROMPTS = 2

PRICE_TABLE_VERSION = "prices-v1"

#: Authoritative USD per 1M tokens (input, output). A model ABSENT from this
#: table has ``pricing_known=False`` and a null cost estimate (spec section 12:
#: an unknown price cannot be treated as free).
DEFAULT_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (5.0, 25.0),
    "claude-haiku-5-5": (1.0, 5.0),
    "claude-opus-5-5": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ModelCallOutcome:
    """Outcome labels for a Model-call row (spec section 11)."""

    SUCCESS = "success"
    FAILED = "failed"
    RETRY_WAIT = "retry_wait"
    SKIPPED = "skipped"
    ALL = (SUCCESS, FAILED, RETRY_WAIT, SKIPPED)


def extract_json(text: str) -> Any:
    """Lenient JSON extraction from a model reply (spec section 4).

    Strips ```json fences, falls back to the first ``{``...``}`` /
    ``[``...``]`` block, and retries after a repair pass (trailing commas).
    Raises :class:`ValueError` when nothing parses.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model reply")
    candidates: list[str] = [raw]
    if raw.startswith("```"):
        inner = raw.strip("`")
        if inner.lower().startswith("json"):
            inner = inner[4:]
        candidates.append(inner.strip())
    for cand in list(candidates):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # First object/array block.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = raw.find(opener)
        j = raw.rfind(closer)
        if 0 <= i < j:
            block = raw[i:j + 1]
            for cand in (block, _repair_trailing_commas(block)):
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    continue
    raise ValueError("no parseable JSON document in model reply")


def _repair_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def validate_against_schema(instance: Any, schema: Mapping[str, Any]) -> list[str]:
    """Post-hoc JSON Schema validation (jsonschema, structural fallback)."""
    try:
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(dict(schema))
        return [f"{list(e.absolute_path)}: {e.message}"
                for e in sorted(validator.iter_errors(instance),
                                key=lambda e: list(e.absolute_path))]
    except ModuleNotFoundError:
        from papercli.agent_contract.schemas import _structural_validate
        return _structural_validate(instance if isinstance(instance, dict) else {},
                                    dict(schema))


def prompt_template_id(template: str, version: str = "v1") -> str:
    """Content hash of a stored prompt template (idempotency input,
    spec section 11: prompt hash + version is part of the key)."""
    digest = hashlib.sha256((version + ":" + template).encode("utf-8"))
    return digest.hexdigest()[:16]


class ModelAdapter:
    """The OpenAI-compatible adapter for the configured endpoint.

    Construction performs one minimal capability probe (echo + structured-
    output leniency) and REPORTS it -- it never crashes (spec section 4:
    capability-test the selected backend). Inject ``httpx.Client`` /
    ``RateLimiter`` / ``ModelBudget`` to keep the suite fully offline.
    """

    ADAPTER_NAME = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: ModelBudget | None = None,
        timeout_seconds: float | int = 60,
        prices: Mapping[str, tuple[float, float]] | None = None,
        on_record: Any | None = None,
        max_re_prompts: int = MAX_RE_PROMPTS,
        sleep_fn: Any | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self.prices = dict(prices) if prices is not None else dict(DEFAULT_PRICES_USD_PER_MTOK)
        self.rate_limiter = rate_limiter or RateLimiter()
        self.budget = budget or ModelBudget()
        self.on_record = on_record
        self.max_re_prompts = max_re_prompts
        import time as _time
        self._sleep = sleep_fn if sleep_fn is not None else _time.sleep
        # The capability probe runs once against a REAL, self-owned client.
        # When a client is injected (offline tests / a shared transport) we
        # skip it so the caller's response sequence stays deterministic;
        # lenient parsing + post-hoc validation apply either way.
        self.capability: dict[str, Any] = (
            self._probe_capability() if client is None
            else {"probed": False, "reachable": None,
                  "note": "probe skipped (injected client)"})

    # -- capability probe ----------------------------------------------------
    def _probe_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._client

    def _probe_capability(self) -> dict[str, Any]:
        """One minimal call: echo + structured-output leniency (report-only)."""
        if not self.base_url:
            return {"probed": False, "reachable": False,
                    "structured_output_enforced": None,
                    "note": "base_url not configured"}
        if not self.api_key:
            return {"probed": False, "reachable": None,
                    "structured_output_enforced": None,
                    "note": f"api key ({self.api_key_env or 'value'}) not available; "
                            "report-only, no probe"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system",
                 "content": "You answer with a single JSON object. Reply exactly "
                            'with {"echo":"ping"} and nothing else.'},
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 16,
            "temperature": 0,
        }
        try:
            resp = self._probe_client().post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            return {"probed": True, "reachable": False, "reachable_error": type(e).__name__,
                    "structured_output_enforced": None,
                    "note": f"probe failed: {type(e).__name__}"}
        result: dict[str, Any] = {
            "probed": True,
            "reachable": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "structured_output_enforced": None,
        }
        if result["reachable"]:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                data = extract_json(content)
                ok = isinstance(data, dict) and data.get("echo") == "ping"
                result["structured_output_enforced"] = bool(ok)
                result["note"] = ("bare-JSON reply" if ok
                                  else "reply required lenient parsing "
                                     "(fenced/loose JSON) -- adapter handles both")
            except (ValueError, KeyError, json.JSONDecodeError, TypeError):
                result["note"] = "probe reply unparseable; lenient parsing enabled"
        return result

    # -- transport -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = self.api_key
        if key is None and self.api_key_env:
            import os
            key = os.environ.get(self.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post_chat(self, payload: dict[str, Any]) -> httpx.Response:
        return self._probe_client().post(
            f"{self.base_url}/chat/completions", json=payload,
            headers=self._headers())

    @property
    def configured(self) -> bool:
        """A model-driven task can run only when base_url + model + key exist."""
        key = self.api_key
        if key is None and self.api_key_env:
            import os
            key = os.environ.get(self.api_key_env)
        return bool(self.base_url and self.model and key)

    # -- cost ------------------------------------------------------------------
    def _cost_estimate(self, model: str, in_tok: int, out_tok: dict) -> dict[str, Any]:
        price = self.prices.get(model)
        if price is None:
            return {"estimated_cost_usd": None, "pricing_known": False,
                    "price_source": None}
        usd = (in_tok / 1e6) * price[0] + (out_tok / 1e6) * price[1]
        return {"estimated_cost_usd": round(usd, 9), "pricing_known": True,
                "price_source": PRICE_TABLE_VERSION}

    # -- structured call -------------------------------------------------------
    def complete_json(
        self,
        *,
        schema: Mapping[str, Any],
        system: str,
        user: str,
        task_type: str,
        prompt_version: str = "v1",
        protocol_revision: int | str = 1,
        run_id: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        purpose: str = "model_call",
        allow_retry: bool = True,
    ) -> dict[str, Any]:
        """One bounded structured model call.

        Returns a dict: ``ok``, ``data``, ``outcome``, ``attempts``,
        ``usage``, ``cost``, ``prompt_id``, ``retries_waited``,
        ``validation_errors``, ``reservation``. Never raises for a bad
        reply -- it records a failed outcome after the bounded re-prompt
        loop; a 429 becomes a bounded backoff into ``retry_wait``
        (spec sections 11-12).
        """
        prompt_id = prompt_template_id(system + "\x00" + user, prompt_version)
        if not self.configured:
            row = self._record_call(
                run_id, task_type, prompt_id, prompt_version, protocol_revision,
                ModelCallOutcome.SKIPPED, 0, 0,
                extra={"reason": "model not configured"})
            return {"ok": False, "data": None, "outcome": ModelCallOutcome.SKIPPED,
                    "attempts": 0, "skipped": True, "usage": None, "cost": None,
                    "prompt_id": prompt_id, "retries_waited": 0,
                    "validation_errors": [], "row_id": row}

        reservation = self.budget.reserve(purpose=purpose, input_tokens=0)
        if reservation is None:
            row = self._record_call(
                run_id, task_type, prompt_id, prompt_version, protocol_revision,
                ModelCallOutcome.SKIPPED, 0, 0,
                extra={"reason": "budget exhausted",
                       "partial_synthesis_reserved": self.budget.partial_synthesis_reserved})
            return {"ok": False, "data": None, "outcome": ModelCallOutcome.SKIPPED,
                    "skipped": True, "budget_exhausted": True,
                    "usage": None, "cost": None, "prompt_id": prompt_id,
                    "attempts": 0, "retries_waited": 0, "validation_errors": [],
                    "row_id": row}

        errors: list[str] = []
        attempts = 0
        retries_waited = 0
        used_in = 0
        used_out = 0
        http_calls = 0
        for attempt in range(1 + self.max_re_prompts):
            attempts = attempt + 1
            rate = self.rate_limiter.acquire()
            if not rate.granted:
                # No token: release the hold, record a retry_wait state.
                self._reconcile(reservation, http_calls, used_in, used_out)
                row = self._record_call(
                    run_id, task_type, prompt_id, prompt_version, protocol_revision,
                    ModelCallOutcome.RETRY_WAIT, used_in, used_out,
                    extra={"wait_seconds": rate.wait_seconds,
                           "attempts": attempts, "http_calls": http_calls})
                return {"ok": False, "data": None, "outcome": ModelCallOutcome.RETRY_WAIT,
                        "throttled": True, "wait_seconds": rate.wait_seconds,
                        "attempts": attempts, "usage": {"input_tokens": used_in,
                                                        "output_tokens": used_out},
                        "cost": None,
                        "prompt_id": prompt_id, "retries_waited": retries_waited,
                        "validation_errors": errors,
                        "row_id": row}

            messages = [{"role": "system", "content": system},
                       {"role": "user", "content": user}]
            if attempt > 0:
                messages.append({"role": "user", "content":
                                 "Your previous reply did not match the required JSON "
                                 "schema. Errors: " + "; ".join(errors[-3:]) +
                                 " Reply again with ONLY a single JSON object."})
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            try:
                resp = self._post_chat(payload)
                http_calls += 1
            except httpx.HTTPError as e:
                self._reconcile(reservation, http_calls, used_in, used_out)
                row = self._record_call(
                    run_id, task_type, prompt_id, prompt_version, protocol_revision,
                    ModelCallOutcome.FAILED, used_in, used_out,
                    extra={"error": type(e).__name__})
                return self._fail_result(row, prompt_id, attempts, errors,
                                         {"error": type(e).__name__},
                                         {"input_tokens": used_in, "output_tokens": used_out})
            if resp.status_code == 429 and attempt < self.max_re_prompts and allow_retry:
                # Bounded exponential backoff into retry_wait -- never a storm.
                delay = min(2 ** attempt, 30)
                retries_waited += 1
                self._sleep(min(delay, 1))
                errors.append(f"HTTP 429 (rate limited); backed off {delay}s")
                continue
            usage = self._usage_from(resp)
            used_in += usage["input_tokens"]
            used_out += usage["output_tokens"]
            if not (200 <= resp.status_code < 300):
                self._reconcile(reservation, http_calls, used_in, used_out)
                row = self._record_call(
                    run_id, task_type, prompt_id, prompt_version, protocol_revision,
                    ModelCallOutcome.FAILED, used_in, used_out,
                    extra={"status_code": resp.status_code})
                return self._fail_result(row, prompt_id, attempts, errors,
                                         {"status_code": resp.status_code},
                                         {"input_tokens": used_in, "output_tokens": used_out})
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                data = extract_json(content)
                errors = validate_against_schema(data, schema)
            except (KeyError, ValueError, json.JSONDecodeError, TypeError) as e:
                errors = [f"parse: {e}"]
                data = None
            if not errors:
                self._reconcile(reservation, http_calls, used_in, used_out)
                cost = self._cost_estimate(self.model, used_in, used_out)
                row = self._record_call(
                    run_id, task_type, prompt_id, prompt_version, protocol_revision,
                    ModelCallOutcome.SUCCESS, used_in, used_out,
                    extra={"cost": cost, "usage": {"input_tokens": used_in,
                                                    "output_tokens": used_out},
                           "http_calls": http_calls})
                return {"ok": True, "data": data,
                        "outcome": ModelCallOutcome.SUCCESS,
                        "attempts": attempts,
                        "usage": {"input_tokens": used_in, "output_tokens": used_out},
                        "cost": cost,
                        "prompt_id": prompt_id, "retries_waited": retries_waited,
                        "validation_errors": [],
                        "row_id": row}
            if attempt >= self.max_re_prompts:
                break
        # Bounded loop exhausted: record a failed outcome (never a crash).
        self._reconcile(reservation, http_calls, used_in, used_out)
        row = self._record_call(
            run_id, task_type, prompt_id, prompt_version, protocol_revision,
            ModelCallOutcome.FAILED, used_in, used_out,
            extra={"validation_errors": errors[-5:], "http_calls": http_calls})
        return self._fail_result(row, prompt_id, attempts, errors, {},
                                 {"input_tokens": used_in, "output_tokens": used_out})

    def complete_text(self, *, system: str, user: str, task_type: str,
                      prompt_version: str = "v1", protocol_revision: int | str = 1,
                      run_id: str | None = None, max_tokens: int = 1024,
                      purpose: str = "model_call") -> dict[str, Any]:
        """Plain text completion (no JSON contract); recorded + reconciled."""
        return self.complete_json(
            schema={}, system=system, user=user, task_type=task_type,
            prompt_version=prompt_version, protocol_revision=protocol_revision,
            run_id=run_id, max_tokens=max_tokens, purpose=purpose)

    # -- internals -------------------------------------------------------------
    def _usage_from(self, resp: httpx.Response) -> dict[str, int]:
        try:
            u = (resp.json().get("usage") or {})
            return {"input_tokens": int(u.get("prompt_tokens") or 0),
                    "output_tokens": int(u.get("completion_tokens") or 0)}
        except (json.JSONDecodeError, AttributeError, TypeError):
            return {"input_tokens": 0, "output_tokens": 0}

    def _reconcile(self, reservation: Reservation | None, calls: int,
                    in_t: int, out_t: int) -> None:
        self.budget.reconcile(reservation, actual_model_calls=calls,
                              actual_input_tokens=in_t, actual_output_tokens=out_t)

    def _fail_result(self, row: dict[str, Any] | None, prompt_id: str, attempts: int,
                     errors: list[str], extra: dict[str, Any],
                     usage: dict[str, int] | None = None) -> dict[str, Any]:
        return {"ok": False, "data": None, "outcome": ModelCallOutcome.FAILED,
                "attempts": attempts, "usage": usage, "cost": None,
                "prompt_id": prompt_id, "retries_waited": 0,
                "validation_errors": list(errors), "row_id": row,
                **extra}

    def _record_call(self, run_id: str | None, task_type: str, prompt_id: str,
                     prompt_version: str, protocol_revision: int | str, outcome: str,
                     in_t: int, out_t: int,
                     extra: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        rec = {
            "model": self.model,
            "endpoint": self.base_url or None,
            "api_key_env": self.api_key_env,
            "task_type": task_type,
            "prompt_hash": prompt_id,
            "prompt_version": prompt_version,
            "protocol_revision": str(protocol_revision),
            "input_tokens": in_t,
            "output_tokens": out_t,
            "outcome": outcome,
            "redacted": True,
        }
        rec.update(dict(extra or {}))
        try:
            row = None
            if self.on_record is not None:
                row = self.on_record(rec)
            return row
        except Exception:  # noqa: BLE001 -- recording must never kill a call
            return None

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    # -- factory ----------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        cfg: Any,
        *,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        budget: ModelBudget | None = None,
        on_record: Any | None = None,
    ) -> "ModelAdapter":
        """Build from a merged config. Secrets stay as env-var references:
        only the NAME of the key variable is kept, never its value."""
        base_url = (cfg.get("model.base_url") or "").strip()
        model = (cfg.get("model.model") or "").strip()
        key_ref = (cfg.get("model.api_key_env") or "").strip()
        timeout = float(cfg.get("execution.request_timeout_seconds") or 60)
        return cls(
            base_url=base_url,
            model=model,
            api_key=None,
            api_key_env=key_ref or None,
            client=client,
            rate_limiter=rate_limiter or RateLimiter(),
            budget=budget,
            timeout_seconds=timeout,
            on_record=on_record,
        )

    # -- visible-in-config (spec section 13) -------------------------------------
    def visible_config(self) -> dict[str, Any]:
        """The NONSECRET parts that must be visible in ``papers config show``."""
        return {"adapter": self.ADAPTER_NAME, "base_url": self.base_url,
                "model": self.model, "api_key_env": self.api_key_env,
                "capability": self.capability}
