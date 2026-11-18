"""Typed in-process models.

The published agent contract is the JSON Schemas under
:mod:`papercli.agent_contract`. These pydantic models mirror the key
envelope/plan/task shapes for in-process typing and validation. The JSON
Schemas remain the authoritative published contract.
"""


# Phase 5: the one-tested OpenAI-compatible model layer + global rate
# limiter + per-run budget reservations (spec sections 4, 11, 12).
from papercli.models.adapter import (
    ModelAdapter,
    ModelCallOutcome,
    extract_json,
    prompt_template_id,
    validate_against_schema,
)
from papercli.models.rate_limit import (
    ModelBudget,
    RateLimiter,
    RateWait,
    DEFAULT_RPM,
    DEFAULT_BUDGETS,
)

__all__ = [
    "ModelAdapter",
    "ModelCallOutcome",
    "extract_json",
    "prompt_template_id",
    "validate_against_schema",
    "ModelBudget",
    "RateLimiter",
    "RateWait",
    "DEFAULT_RPM",
    "DEFAULT_BUDGETS",
]
