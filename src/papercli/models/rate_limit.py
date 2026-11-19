"""Global model rate limiting + per-run budget reservations.

Spec section 12: provider rate limits stay SEPARATE from model concurrency
limits. This module holds the *model-side* layer:

* :class:`RateLimiter` -- a token bucket that caps the configured
  OpenAI-compatible endpoint (user endpoint is rate-limited to 60 req/min;
  the default 45 req/min leaves headroom). It is GLOBAL (one per endpoint)
  and separate from any provider rate limiter.
* :class:`ModelBudget` -- the per-run hard ceilings (calls / input tokens /
  output tokens / estimated USD). Budgets are enforced BEFORE dispatch using
  reservations; actual usage is reconciled against the reservation when the
  call returns (spec section 12 "reconcile with returned usage").
* Budget exhaustion stops new dispatches, but a hard reservation for a
  partial synthesis + status bundle stays available (spec section 12:
  "reserve time/tokens for a partial synthesis and status bundle").
* Dollar figures are ESTIMATES from a known price table; an unknown price
  is recorded as ``cost=None`` + ``pricing_known=False`` and is NEVER
  treated as free (spec section 12).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "RateLimiter",
    "RateWait",
    "ModelBudget",
    "Reservation",
    "DEFAULT_RPM",
    "DEFAULT_BUDGETS",
    "PARTIAL_SYNTHESIS_RESERVATION",
]

#: Default global model rate limit. The configured endpoint is capped at
#: 60 req/min; 45 req/min is the documented default (user-configurable).
DEFAULT_RPM = 45

#: Documented default hard ceilings (spec section 7 example ceilings; a null
#: USD cap is allowed ONLY with hard call/token ceilings -- the user has no
#: dollar policy, so the estimate cap stays None and tokens/calls govern).
DEFAULT_BUDGETS: dict[str, int | float | None] = {
    "max_model_calls": 300,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 150_000,
    "max_estimated_cost_usd": None,
}

#: Reserved slice for a partial synthesis + status bundle when the budget
#: exhausts (spec section 12).
PARTIAL_SYNTHESIS_RESERVATION = {
    "model_calls": 6,
    "input_tokens": 20_000,
    "output_tokens": 4_000,
}


@dataclass(frozen=True)
class RateWait:
    """Outcome of asking the limiter for a token."""
    granted: bool
    wait_seconds: float = 0.0

    @property
    def throttled(self) -> bool:
        return not self.granted


class RateLimiter:
    """Token-bucket limiter for the configured model endpoint.

    Default: 45 requests/minute (one token per 1/45 minute of refill,
    capacity 45). Tests can scale ``requests_per_minute`` / ``capacity``.
    ``now_fn`` / ``wait_fn`` are injectable so the suite is offline.
    """

    def __init__(
        self,
        requests_per_minute: int = DEFAULT_RPM,
        capacity: int | None = None,
        *,
        now_fn: Callable[[], float] = time.monotonic,
        wait_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rpm = int(requests_per_minute)
        self.capacity = int(capacity if capacity is not None else self.rpm)
        self.tokens = float(self.capacity)
        self.refill_per_second = self.rpm / 60.0
        self._last = now_fn()
        self._now = now_fn
        self._wait = wait_fn

    def _refill(self) -> None:
        now = self._now()
        elapsed = now - self._last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self._last = now

    def acquire(self) -> RateWait:
        """Try to take one token without blocking."""
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return RateWait(True, 0.0)
        needed = 1.0 - self.tokens
        wait = needed / self.refill_per_second if self.refill_per_second > 0 else 0.0
        return RateWait(False, round(wait, 3))

    def acquire_wait(self, max_wait_seconds: float | None = None) -> RateWait:
        """Take one token, sleeping (bounded) until one is available."""
        deadline = self._now() + (max_wait_seconds if max_wait_seconds is not None else 30.0)
        while True:
            r = self.acquire()
            if r.granted:
                return r
            now = self._now()
            if now + r.wait_seconds > deadline:
                return RateWait(False, r.wait_seconds)
            self._wait(r.wait_seconds)

    def wait_seconds(self) -> float:
        """Seconds until the next token is available, WITHOUT consuming one.

        Pure query: refills to the current instant and reports the deficit
        wait. Does NOT decrement the bucket (unlike ``acquire``).
        """
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        needed = 1.0 - self.tokens
        return round(needed / self.refill_per_second, 3) if self.refill_per_second > 0 else 0.0


@dataclass
class Reservation:
    """A pre-dispatch budget hold, reconciled when usage returns."""
    reservation_id: str
    model_calls: int
    input_tokens: int
    output_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class ModelBudget:
    """Per-run hard ceilings enforced via reservations (spec section 12).

    Invariant: the reservable budget on any dimension is
    ``ceiling - used_total - open_reservations``. ``reserve`` holds budget
    BEFORE dispatch; ``reconcile`` releases the hold and bills the
    returned usage (usage beyond the reservation is a *deficit*, charged
    against the pool and disclosed -- not silently absorbed).

    On exhaustion (actual usage reaches a hard ceiling): new normal
    dispatches stop, and the closure slice for a partial synthesis +
    status bundle is reserved automatically (capped at the remaining
    budget -- a degenerate zero-slice is a valid outcome; the run still
    closes with a labeled partial bundle, spec section 12).
    """

    _DIMS = ("model_calls", "input_tokens", "output_tokens")

    def __init__(self, limits: dict[str, Any] | None = None) -> None:
        merged: dict[str, Any] = dict(DEFAULT_BUDGETS)
        merged.update(limits or {})
        self.limits = merged
        self.ceiling: dict[str, int | None] = {
            "model_calls": merged.get("max_model_calls"),
            "input_tokens": merged.get("max_input_tokens"),
            "output_tokens": merged.get("max_output_tokens"),
        }
        self.actual = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.reservations: list[Reservation] = []
        self._exhausted = False
        self.partial_synthesis_reserved = False
        #: An estimate cap, reported but NOT enforced per call: without a
        #: known price a dollar figure cannot be spent (unknown price
        #: cannot be treated as free -- spec section 12).
        self.max_estimated_cost_usd = merged.get("max_estimated_cost_usd")

    # -- introspection -----------------------------------------------------
    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def open_reserved(self, key: str) -> int:
        field = {"model_calls": "model_calls", "input_tokens": "input_tokens",
                 "output_tokens": "output_tokens"}[key]
        return sum(getattr(r, field) for r in self.reservations)

    def free(self, key: str) -> int | None:
        """Reservable budget left on ``key`` (None = unbounded)."""
        cap = self.ceiling.get(key)
        if cap is None:
            return None
        return cap - self.actual[key] - self.open_reserved(key)

    def remaining(self) -> dict[str, int | None]:
        return {k: self.free(k) for k in self._DIMS}

    # -- reserve / reconcile ----------------------------------------------
    def reserve(self, *, model_calls: int = 1,
                input_tokens: int = 0, output_tokens: int = 0,
                purpose: str = "model_call") -> Reservation | None:
        """Reserve budget for a dispatch. ``None`` -> cannot dispatch.

        On a failed normal reservation the exhaustion flag is set and the
        closure slice is reserved (spec section 12).
        """
        if purpose in ("partial_synthesis", "status_bundle"):
            return self._reserve_slice(purpose)
        self._check_hard_floor()
        if self._exhausted:
            return None
        wants = {"model_calls": model_calls, "input_tokens": input_tokens,
                 "output_tokens": output_tokens}
        for k, want in wants.items():
            avail = self.free(k)
            if avail is not None and want > avail:
                self._mark_exhausted()
                return None
        res = Reservation(f"res-{uuid.uuid4()}", model_calls, input_tokens,
                          output_tokens)
        self.reservations.append(res)
        return res

    def reconcile(self, reservation: Reservation | None, *, actual_model_calls: int,
                  actual_input_tokens: int, actual_output_tokens: int) -> dict[str, Any]:
        """Bill returned usage and release the hold (spec section 12).

        A *deficit* (usage beyond the reservation) is charged against the
        pool and reported; it is never silently absorbed.
        """
        deficit = False
        if reservation is not None:
            if reservation not in self.reservations:
                raise ValueError("unknown reservation")
            self.reservations.remove(reservation)
            deficit = (actual_model_calls > reservation.model_calls
                       or actual_input_tokens > reservation.input_tokens
                       or actual_output_tokens > reservation.output_tokens)
        self.actual["model_calls"] += actual_model_calls
        self.actual["input_tokens"] += actual_input_tokens
        self.actual["output_tokens"] += actual_output_tokens
        self._check_hard_floor()
        return {"reservation_id": reservation.reservation_id if reservation else None,
                "deficit": deficit}

    def reserve_partial_synthesis(self,
                                  slice_: dict[str, int] | None = None) -> Reservation | None:
        """Reserve the partial-synthesis + status slice (spec section 12)."""
        s = dict(slice_ or PARTIAL_SYNTHESIS_RESERVATION)
        return self._reserve_slice(
            "partial_synthesis",
            calls=int(s.get("model_calls", 1)),
            in_t=int(s.get("input_tokens", 0)),
            out_t=int(s.get("output_tokens", 0)))

    def allow_dispatch(self, *, purpose: str = "model_call") -> bool:
        """Whether a NEW dispatch of ``purpose`` may start (fail-closed).

        * normal model calls: only while the budget is not exhausted;
        * ``partial_synthesis`` / ``status_bundle``: always allowed -- the
          reserved closure path (spec section 12). The reservation itself
          is still capped at the remaining budget.

        Pure check: no mutation. The dispatch must still ``reserve``.
        """
        if purpose in ("partial_synthesis", "status_bundle"):
            return True
        self._check_hard_floor()
        return not self._exhausted

    # -- internals -----------------------------------------------------------
    def _reserve_slice(self, purpose: str, *, calls: int | None = None,
                       in_t: int | None = None, out_t: int | None = None) -> Reservation | None:
        if purpose == "status_bundle":
            res = Reservation(f"res-{uuid.uuid4()}", 0, 0, 0)
            self.reservations.append(res)
            return res
        if calls is None:
            s = dict(PARTIAL_SYNTHESIS_RESERVATION)
            calls, in_t, out_t = int(s["model_calls"]), int(s["input_tokens"]), int(s["output_tokens"])
        # The slice is capped at the remaining budget; on exhaustion this
        # yields a degenerate (zero) slice -- still a valid, labeled
        # partial-closure reservation.
        def cap(v: int, key: str) -> int:
            have = self.free(key)
            return v if have is None else min(v, max(0, have))
        res = Reservation(f"res-{uuid.uuid4()}",
                          cap(calls, "model_calls"),
                          cap(in_t, "input_tokens"),
                          cap(out_t, "output_tokens"))
        self.reservations.append(res)
        if purpose == "partial_synthesis":
            self.partial_synthesis_reserved = True
        return res

    def _check_hard_floor(self) -> None:
        if self._exhausted:
            return
        if any(self.ceiling.get(k) is not None and self.actual[k] >= self.ceiling[k]
               for k in self._DIMS):
            self._mark_exhausted()

    def mark_exhausted(self) -> None:
        """Public: force the exhaustion flag + reserve the closure slice.

        Idempotent. Phase 6 uses this when restoring a run whose persisted
        usage already sits at a hard ceiling, so a resume keeps the closure
        slice reserved (spec section 12) instead of re-opening dispatch."""
        self._mark_exhausted()

    def _mark_exhausted(self) -> None:
        if self._exhausted:
            return
        self._exhausted = True
        # Spec section 12: on exhaustion reserve the partial-synthesis slice
        # (capped at what remains) so the run can still close with a
        # labeled partial bundle + status.
        self.reserve_partial_synthesis()

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": self.limits,
            "remaining": self.remaining(),
            "actual": self.actual,
            "exhausted": self._exhausted,
            "partial_synthesis_reserved": self.partial_synthesis_reserved,
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
            "open_reservations": [r.to_dict() for r in self.reservations],
        }
