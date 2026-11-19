
"""Standalone overnight-worker orchestrator (Phase 6, spec sections 4/5/11/12).

Runs ONE planned review Run to a durable closure state, driven through the
Phase-3 :class:`~papercli.jobs.tasks.TaskQueue` lease protocol:

* dispatches the run's task DAG (deterministic research layers + the Phase-5
  model-driven reasoning tasks), one leased task at a time;
* enforces the run's hard limits -- the model budget is a SHARED
  :class:`~papercli.models.rate_limit.ModelBudget` (reservations before
  dispatch, reconciliation after, deficit surfaced -- spec section 12);
* on a wall-clock deadline or budget exhaustion stops new dispatch and
  reserves the closure slice for a partial synthesis + status bundle
  (spec section 12);
* checkpoints after EVERY task (spec section 11), so an interrupted run
  resumes from the next pending task and never re-does completed work;
* idempotent: model-task keys embed protocol revision + prompt version, so
  a re-dispatch can never silently reuse an obsolete interpretation
  (spec section 11).

The worker is process-independent of the launcher: it writes a durable
handshake + heartbeat (the worker log dir under the storage tree), which is
how status tells RUNNING apart from STALE-possibly-interrupted -- a PID
alone is never trusted (spec section 12).
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from papercli.jobs.tasks import TaskQueue
from papercli.models.rate_limit import ModelBudget, RateLimiter
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "DAG_VERSION",
    "TASK_TYPES",
    "TaskError",
    "ReasoningTaskRunner",
    "RunWorker",
]

#: The worker's DAG layout version. Deterministic task idempotency keys
#: embed this; model-driven tasks key on protocol revision + prompt version
#: instead (spec section 11: a changed interpretation yields a NEW task
#: key -- an obsolete model result is never silently reused).
DAG_VERSION = "dag-v1"

#: The Phase-5 model-driven task types (they run after the deterministic
#: layers that feed them).
_REASONING_TASKS = ("screen_uncertain", "extract_findings", "synthesize", "critique")

#: Every task type in one run DAG (deterministic + reasoning).
TASK_TYPES = ("discover", "screen", "expand", "retrieval", "evidence",
              *_REASONING_TASKS)

#: Bounded retry backoff for ordinary task failures (jitter + delay,
#: spec section 12: long waits are DEFERRED through scheduler state --
#: the task's ``next_attempt_at`` -- never a storm).
_DEFAULT_RETRY_DELAY_SECONDS = 5.0
_DEFAULT_RETRY_JITTER_SECONDS = 5.0

#: Idle polls of a future-due retry_wait task before the run closes
#: partial (the deferral lives in the task's scheduler state; a resume
#: picks it up when it is due -- spec section 12).
_MAX_IDLE_POLLS = 60


class TaskError(Exception):
    """A task cannot complete right now (model failure, provider outage...)."""

    def __init__(self, message: str, result: Mapping[str, Any] | None = None):
        super().__init__(message)
        #: A completed-but-unusable result (e.g. a RETRY_WAIT outcome the
        #: scheduler defers through the task's next_attempt_at).
        self.result = dict(result or {})


# ---------------------------------------------------------------------------
# Config reconstruction (secrets stay env-var references, spec section 13)
# ---------------------------------------------------------------------------

def _snapshot_config(store: PaperStore, run_id: str) -> Any:
    """The run's effective config from its saved snapshot (nonsecret).

    Secret values are resolved from the process environment (the detached
    child inherits it); the snapshot stores only reference names.
    """
    row = store.run_row(run_id) or {}
    snap_ref = row.get("config_snapshot_ref") or ""
    if not snap_ref:
        return None
    snap_path = Path(store.db_path).parent / snap_ref
    if not snap_path.exists():
        return None
    from papercli.config.loader import Config
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return Config(dict(snap.get("effective") or {}),
                  sources=[f"snapshot:{snap_ref}"], env=dict(os.environ))


def _model_config(store: PaperStore, run_id: str) -> Any:
    """The run's model config, or ``None`` (agent-driven mode, spec 4)."""
    cfg = _snapshot_config(store, run_id)
    if cfg is None:
        return None
    if not (cfg.get("model.base_url") and cfg.get("model.model")):
        return None
    return cfg


def _task_key(task_type: str, payload: Mapping[str, Any]) -> str:
    if task_type in _REASONING_TASKS:
        proto = payload.get("protocol_revision") or "1"
        return (f"{task_type}:{payload.get('input_tag')}:{proto}"
                f":{payload.get('prompt_version')}")
    return f"{task_type}:{payload.get('input_tag')}:{DAG_VERSION}"


# ---------------------------------------------------------------------------
# Model-driven task execution
# ---------------------------------------------------------------------------

class _TrackingModelAdapter:
    """Wraps the run's shared adapter: records the last call outcome + the
    budget-deficit flag so the runner can defer/surface them (spec 12)."""

    def __init__(self, inner: Any, runner: "ReasoningTaskRunner"):
        self._inner = inner
        self._runner = runner
        self.last_outcome: str | None = None

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        res = self._inner.complete_json(**kwargs)
        self.last_outcome = res.get("outcome")
        self._runner.last_deficit = bool(
            self._runner.last_deficit or res.get("budget_deficit"))
        return res

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ReasoningTaskRunner:
    """Runs the Phase-5 reasoning tasks against the SHARED run budget.

    ``model_cfg`` is ``None`` in agent-driven mode (no model endpoint
    configured): no model task is dispatchable and the run continues
    deterministically (spec section 4). The adapter binds the run's shared
    :class:`ModelBudget` + :class:`RateLimiter`, so every model call is
    reserved BEFORE dispatch and reconciled after (spec section 12).

    ``adapter_factory`` is the offline injection point: a callable
    ``(cfg=..., store=..., run_id=..., limits=..., shared_budget=...,
    rate_limiter=...) -> ModelAdapter``.
    """

    def __init__(self, model_cfg: Any, shared_budget: ModelBudget,
                 rate_limiter: RateLimiter,
                 adapter_factory: Callable[..., Any] | None = None):
        self.model_cfg = model_cfg
        self.shared_budget = shared_budget
        self.rate_limiter = rate_limiter
        self.adapter_factory = adapter_factory
        self._adapter: Any = None
        self.last_deficit = False

    def _build_adapter(self, store: PaperStore, run_id: str) -> Any:
        limits = dict(self.model_cfg.get("limits") or {}) if self.model_cfg else {}

        def rec(rec: Mapping[str, Any]) -> Any:
            if store is None or run_id is None:
                return None
            return store.record_model_call(
                run_id,
                model=rec.get("model", ""),
                outcome=rec.get("outcome", "failed"),
                task_type=rec.get("task_type"),
                prompt_hash=rec.get("prompt_hash"),
                prompt_version=rec.get("prompt_version"),
                protocol_revision=rec.get("protocol_revision"),
                input_tokens=rec.get("input_tokens"),
                output_tokens=rec.get("output_tokens"),
                estimated_cost_usd=(rec.get("cost") or {}).get("estimated_cost_usd"),
                usage={**(rec.get("cost") or {}),
                       **(rec.get("usage") or {})},
                endpoint=rec.get("endpoint"),
                api_key_env=rec.get("api_key_env"),
            )

        if self.adapter_factory is not None:
            return self.adapter_factory(cfg=self.model_cfg, store=store,
                                        run_id=run_id, limits=limits,
                                        shared_budget=self.shared_budget,
                                        rate_limiter=self.rate_limiter)
        # Production path: one shared adapter for the whole run (one client,
        # no per-task capability probes; the Phase-5 doctor owns probing).
        from papercli.models.adapter import ModelAdapter
        import httpx
        cfg = self.model_cfg
        timeout = float((cfg.get("execution.request_timeout_seconds") or 60)
                        if cfg is not None else 60)
        return ModelAdapter.from_config(
            cfg,
            client=httpx.Client(timeout=timeout),  # injected -> no probe
            budget=self.shared_budget,
            rate_limiter=self.rate_limiter,
            on_record=rec,
        )

    def _adapter_for(self, store: PaperStore, run_id: str) -> Any:
        if self._adapter is None:
            self._adapter = _TrackingModelAdapter(
                self._build_adapter(store, run_id), self)
        return self._adapter

    def run_reasoning_task(self, store: PaperStore, run_id: str,
                           task_type: str,
                           task_payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one Phase-5 reasoning task.

        Raises :class:`TaskError` when the outcome is unusable; a RETRY_WAIT
        outcome (bounded 429/rate backoff) defers through scheduler state
        (spec section 12).
        """
        if task_type not in _REASONING_TASKS:
            raise TaskError(f"not a reasoning task: {task_type}")
        import papercli.reasoning as reasoning
        if self.model_cfg is None or not reasoning.model_available(self.model_cfg):
            return reasoning.skipped_result(
                task_type, run_id, note="model not usable; deterministic "
                "layers only (spec section 4)")
        adapter = self._adapter_for(store, run_id)

        def factory(**_kw):
            return adapter

        handler = reasoning.TASKS[task_type]
        result = handler(store, dict(task_payload), cfg=self.model_cfg,
                         adapter_factory=factory)
        self.last_deficit = self.last_deficit or bool(
            getattr(adapter, "last_budget_deficit", False))
        status = result.get("status")
        # The outcome is the task's own field when it carries one
        # (extract/synthesize/critique), else the tracker's last call
        # outcome (screen_uncertain does not surface it in its result).
        outcome = result.get("model_call_outcome")
        if outcome is None and adapter is not None:
            outcome = getattr(adapter, "last_outcome", None)
        if outcome == "retry_wait":
            # Bounded rate backoff: DEFER through scheduler state with the
            # limiter's own wait estimate (spec section 12: transient
            # retries with jitter/bounded backoff, long waits deferred).
            wait = self.rate_limiter.wait_seconds()
            raise TaskError(
                f"{task_type}: model in retry_wait",
                result={**result, "status": "retry_wait",
                         "wait_seconds": wait})
        if status not in ("succeeded", "skipped_no_model"):
            raise TaskError(f"{task_type}: {status}", result=result)
        return result

    def close(self) -> None:
        if self._adapter is not None and hasattr(self._adapter, "close"):
            self._adapter.close()
            self._adapter = None


# ---------------------------------------------------------------------------
# The run worker
# ---------------------------------------------------------------------------

class RunWorker:
    """Orchestrates one run's task DAG through the shared lease protocol."""

    def __init__(
        self,
        store: PaperStore,
        run_id: str,
        *,
        model_cfg: Any = ...,
        reasoning_runner: ReasoningTaskRunner | None = None,
        adapter_factory: Callable[..., Any] | None = None,
        search_service_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
        now_fn: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        work_dir: Path | str | None = None,
        cancel_request: Path | str | None = None,
        retry_delay_seconds: float = _DEFAULT_RETRY_DELAY_SECONDS,
        retry_jitter_seconds: float = _DEFAULT_RETRY_JITTER_SECONDS,
    ):
        self.store = store
        self.run_id = run_id
        self.queue = TaskQueue(store)
        self.clock = clock or time.monotonic
        self._now_fn = now_fn or utc_now_iso
        self._sleep = sleep
        self._search_factory = search_service_factory
        self._retry_delay = float(retry_delay_seconds)
        self._retry_jitter = float(retry_jitter_seconds)

        row = store.run_row(run_id)
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        self.limits: dict[str, Any] = dict(json.loads(row.get("limits_json") or "{}"))
        if model_cfg is ...:
            model_cfg = _model_config(store, run_id)
        self.model_cfg = model_cfg

        self.shared_budget = self.restore_budget()
        self.rate_limiter = RateLimiter()
        self.runner: ReasoningTaskRunner = reasoning_runner or ReasoningTaskRunner(
            model_cfg=self.model_cfg,
            shared_budget=self.shared_budget,
            rate_limiter=self.rate_limiter,
            adapter_factory=adapter_factory,
        )

        from papercli.exports.run_summary import run_dir
        self.work_dir = Path(work_dir) if work_dir else run_dir(store, run_id)
        self._log_file = self.work_dir / "worker.log.jsonl"
        self._cancel_request = (Path(cancel_request) if cancel_request
                                else self.work_dir / "cancel-request")
        self._deadline_at: str | None = None
        self._cancel_requested = False
        self._deadline_reached = False

    # -- limits / deadline ----------------------------------------------------
    def wall_deadline(self) -> str | None:
        """The run's wall-clock deadline as a durable UTC ISO timestamp.

        Once a worker anchors it, the deadline is CHECKPOINTED and every
        later worker restores that value (never extended, spec section 5:
        an interrupted run keeps its original limits). A resume of a run
        whose anchored deadline already lies in the past therefore stops
        new dispatch immediately and closes the partial bundle."""
        if self._deadline_at is None:
            counters = json.loads(
                (self.store.run_row(self.run_id) or {}).get("counters_json") or "{}")
            prev = (counters.get("worker") or {}).get("deadline_at")
            if prev:
                self._deadline_at = str(prev)
                return self._deadline_at
            minutes = self.limits.get("wall_minutes")
            if minutes:
                # Anchor from this worker's clock so injected test clocks
                # stay in control (production clock = time.monotonic).
                self._deadline_at = self._deadline_from_anchor()
        return self._deadline_at

    def _deadline_from_anchor(self) -> str:
        """Anchor the deadline to the first checkpoint (durable)."""
        counters = json.loads(
            (self.store.run_row(self.run_id) or {}).get("counters_json") or "{}")
        anchor = (counters.get("worker") or {}).get("started_at_wall")
        base = (datetime.fromisoformat(anchor) if anchor
                else datetime.now(timezone.utc))
        minutes = float(self.limits.get("wall_minutes") or 0)
        return (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def deadline_reached(self) -> bool:
        dl = self.wall_deadline()
        if dl is None:
            return False
        try:
            limit_dt = datetime.fromisoformat(dl)
        except ValueError:
            return False
        try:
            now_dt = datetime.fromisoformat(self._now_fn())
        except ValueError:
            return False
        return now_dt >= limit_dt

    def cancel_requested(self) -> bool:
        if self._cancel_request is not None and self._cancel_request.exists():
            self._cancel_requested = True
        return self._cancel_requested

    # -- budget (shared, restored from persisted model-call rows) --------------
    def restore_budget(self) -> ModelBudget:
        """Rebuild the run's hard ceilings from limits + persisted usage.

        Billed rows (``success``/``failed`` outcomes) carry reconciled
        usage; an unpriced model keeps a null cost estimate -- never
        treated as free (spec section 12).
        """
        calls = self.store.model_calls_for_run(self.run_id)
        billed = [c for c in calls if c.get("outcome") in ("success", "failed")]
        actual = {
            "model_calls": len(billed),
            "input_tokens": sum(int(c.get("input_tokens") or 0) for c in calls),
            "output_tokens": sum(int(c.get("output_tokens") or 0) for c in calls),
        }
        budget = ModelBudget(dict(self.limits))
        budget.actual.update(actual)
        if any(
            budget.ceiling.get(k) is not None and actual[k] >= budget.ceiling[k]
            for k in ("model_calls", "input_tokens", "output_tokens")
        ):
            # Already at a hard ceiling: the exhaustion path reserves the
            # closure slice so the run can still close (spec section 12).
            budget.mark_exhausted()
        return budget

    # -- DAG ---------------------------------------------------------------------
    def build_dag(self) -> dict[str, str]:
        """Enqueue the run's task DAG (idempotent). Returns type -> task_id."""
        row = self.store.run_row(self.run_id) or {}
        plan = json.loads(row.get("plan_json") or "{}")
        proto_revision = str(row.get("protocol_revision") or 1)
        queries = [q for q in (plan.get("queries") or [])
                   if q.get("provider") != "(truncated)"]
        base = {"run_id": self.run_id, "protocol_revision": proto_revision,
                "limits": self.limits}
        self._dag: dict[str, str] = {}

        def enqueue(task_type: str, payload: Mapping[str, Any],
                    dependencies: list[str] | None = None) -> str:
            p = {**base, **dict(payload)}
            t = self.queue.enqueue(
                self.run_id, task_type, _task_key(task_type, p),
                payload=p, dependencies=dependencies or None,
                protocol_revision=proto_revision,
            )
            self._dag[task_type] = t.task_id
            return t.task_id

        prev: str | None = None
        for i, q in enumerate(queries):
            prev = enqueue("discover", {
                "input_tag": f"query-{i}",
                "query_index": i,
                "provider": q.get("provider"),
                "query": q.get("query"),
            }, [prev] if prev else [])
        screen_id = enqueue("screen", {"input_tag": "all-discovered"},
                            [prev] if prev else [])
        expand_id = enqueue("expand", {"input_tag": "accepted-papers"},
                            [screen_id])
        screen2_id = enqueue("screen", {"input_tag": "after-expansion"},
                             [expand_id])
        full_text_cap = int(self.limits.get("max_full_text_papers") or 0)
        if full_text_cap > 0:
            retr_id = enqueue("retrieval",
                              {"input_tag": f"fulltext-{full_text_cap}"},
                              [screen2_id])
        else:
            retr_id = screen2_id
        ev_id = enqueue("evidence", {"input_tag": "usable-documents"},
                       [retr_id])
        if self.model_cfg is not None:
            from papercli.reasoning import PROMPT_VERSIONS
            chain = ev_id
            for ttype in _REASONING_TASKS:
                chain = enqueue(
                    ttype,
                    {"input_tag": "run",
                     "prompt_version": PROMPT_VERSIONS.get(ttype)},
                    [chain])
        return self._dag

    # -- task bodies ---------------------------------------------------------------
    def _execute(self, task: Any) -> dict[str, Any]:
        p = dict(task.payload or {})
        ttype = task.type
        if ttype == "discover":
            return self._task_discover(p)
        if ttype == "screen":
            return self._task_screen(p)
        if ttype == "expand":
            return self._task_expand(p)
        if ttype == "retrieval":
            return self._task_retrieval(p)
        if ttype == "evidence":
            return self._task_evidence(p)
        if ttype in _REASONING_TASKS:
            return self.runner.run_reasoning_task(self.store, self.run_id, ttype, p)
        raise TaskError(f"unknown task type: {ttype}")

    def _search_service(self) -> Any:
        if self._search_factory is not None:
            return self._search_factory()
        from papercli.providers.retrieval import SearchService
        try:
            from papercli.providers.cache import RetrievalCache
            cache = RetrievalCache(Path(self.store.db_path).parent / "cache")
        except Exception:  # noqa: BLE001 -- cache is an optimization
            cache = None
        cfg = _snapshot_config(self.store, self.run_id)
        return SearchService(cfg, dict(os.environ), cache=cache,
                             store=self.store)

    def _task_discover(self, p: dict[str, Any]) -> dict[str, Any]:
        """One planned provider query: search + persist (cap-bounded)."""
        service = self._search_service()
        limit = int(p.get("limits", {}).get("records_per_query_estimate") or 50)
        result = service.search(p.get("query") or "", [p.get("provider")],
                                limit=limit)
        # Enforce the discovery cap BEFORE persistence (spec section 12:
        # budgets enforced before dispatch; the cap is a hard ceiling).
        cap = int(p.get("limits", {}).get("max_discovered_records") or 0)
        deferred = 0
        if cap > 0:
            existing = len(self.store.papers_for_run(self.run_id))
            remaining = max(0, cap - existing)
            records = list(result.get("records") or [])
            if len(records) > remaining:
                deferred = len(records) - remaining
                result["records"] = records[:remaining]
                result["record_count"] = len(records)
                result["completeness"] = "partial"
                for prov in result.get("providers") or []:
                    prov.setdefault("deferred_records", deferred)
        service.persist(result, self.run_id)
        return {
            "provider": p.get("provider"),
            "query_index": p.get("query_index"),
            "record_count": result.get("record_count"),
            "deduplicated_from": result.get("deduplicated_from"),
            "completeness": result.get("completeness"),
            "deferred_records": deferred,
            "errors": result.get("errors") or [],
        }

    def _accepted_paper_ids(self) -> set[str]:
        rows = self.store._conn.execute(
            "SELECT DISTINCT s.paper_id FROM screenings s "
            "JOIN papers p ON p.id = s.paper_id "
            "WHERE p.run_id = ? AND s.decision = 'include'",
            (self.run_id,)).fetchall()
        return {r["paper_id"] for r in rows}

    def _latest_document(self, paper_id: str) -> dict[str, Any] | None:
        row = self.store._conn.execute(
            "SELECT * FROM documents WHERE paper_id = ? ORDER BY id DESC LIMIT 1",
            (paper_id,)).fetchone()
        return dict(row) if row is not None else None

    def _task_screen(self, p: dict[str, Any]) -> dict[str, Any]:
        """Deterministic title/abstract screening + dedup (spec section 9).

        Idempotent: papers already decided are skipped, so the
        after-expansion pass screens only the new candidates."""
        from papercli.research.screening import (
            ScreeningPipeline, deduplicate_papers)
        plan = json.loads(
            (self.store.run_row(self.run_id) or {}).get("plan_json") or "{}")
        pipeline = ScreeningPipeline(self.store, self.run_id, plan)
        decided_ids = {s["paper_id"] for s in
                       self.store.screenings_for_run(self.run_id)
                       if s.get("stage") == "title_abstract"}
        records: list[dict[str, Any]] = []
        for row in self.store.papers_for_run(self.run_id):
            if row["id"] in decided_ids:
                continue
            ids = json.loads(row.get("canonical_ids_json") or "{}")
            meta = json.loads(row.get("metadata_provenance_json") or "{}")
            providers = meta.get("providers") or (
                [meta["provider"]] if meta.get("provider") else [])
            records.append({
                "doi": ids.get("doi"),
                "arxiv_id": ids.get("arxiv_id"),
                "s2_id": ids.get("s2"),
                "openalex_id": ids.get("openalex"),
                "title": row.get("title"),
                "authors": json.loads(row.get("authors_json") or "[]"),
                "year": row.get("year"),
                "abstract": None,
                "provider": (providers or [None])[0],
                "also_from": meta.get("also_from"),
            })
        res = pipeline.screen_records(records)
        links = int(deduplicate_papers(self.store, self.run_id) or 0)
        res["links_created"] = links
        res["accepted_paper_ids"] = sorted(self._accepted_paper_ids())
        return res

    def _task_expand(self, p: dict[str, Any]) -> dict[str, Any]:
        """Bounded citation expansion (provenance preserved, spec 8/9)."""
        from papercli.research.expansion import expand_search
        plan = json.loads(
            (self.store.run_row(self.run_id) or {}).get("plan_json") or "{}")
        accepted_ids = self._accepted_paper_ids()
        accepted = [r for r in self.store.papers_for_run(self.run_id)
                    if r["id"] in accepted_ids]
        cfg = _snapshot_config(self.store, self.run_id)
        cache = None
        try:
            from papercli.providers.cache import RetrievalCache
            cache = RetrievalCache(Path(self.store.db_path).parent / "cache")
        except Exception:  # noqa: BLE001
            cache = None
        res = expand_search(self.store, self.run_id, plan, accepted,
                            config=cfg, env=dict(os.environ), cache=cache,
                            adapter_factory=self._expansion_adapters)
        return {
            "seeds": res.get("seeds"),
            "added": res.get("added"),
            "deferred": res.get("deferred"),
            "complete": res.get("complete"),
            "incomplete_branches": res.get("incomplete_branches") or [],
        }

    @staticmethod
    def _expansion_adapters(name: str) -> Any:
        from papercli.providers.registry import get_registry
        import papercli.providers.openalex  # noqa: F401 (register)
        import papercli.providers.semantic_scholar  # noqa: F401
        return get_registry().get(name)

    def _task_retrieval(self, p: dict[str, Any]) -> dict[str, Any]:
        """Fetch accepted papers up to the full-text cap (spec section 8).

        A failed fetch is DATA (a recorded unavailable), not an error.
        Fetched documents are parsed into the stable text index so the
        evidence layer can read them; parse quality caps the access level
        (D-1: scanned/partial documents are never 'fully read')."""
        from papercli.providers.retrieval import fetch_document
        from papercli.research.quality import (
            cap_document_access_level,
            compute_parse_grade,
            has_extracted_text,
        )
        cap = int((self.limits or {}).get("max_full_text_papers") or 0)
        cfg = _snapshot_config(self.store, self.run_id)
        cache = None
        try:
            from papercli.providers.cache import RetrievalCache
            cache = RetrievalCache(Path(self.store.db_path).parent / "cache")
        except Exception:  # noqa: BLE001
            cache = None
        out_dir = (Path((cfg.get("retrieval.out_dir") or
                         str(Path(self.store.db_path).parent / "papers")))
                   if cfg is not None
                   else Path(self.store.db_path).parent / "papers")
        out_dir.mkdir(parents=True, exist_ok=True)
        accepted_ids = self._accepted_paper_ids()
        accepted = [r for r in self.store.papers_for_run(self.run_id)
                    if r["id"] in accepted_ids]

        def fetchable(r: Mapping[str, Any]) -> bool:
            ids = json.loads(r.get("canonical_ids_json") or "{}")
            return bool(ids.get("doi") or ids.get("arxiv_id"))

        accepted.sort(key=lambda r: (not fetchable(r), r["id"]))
        to_fetch = accepted[:max(0, cap)]
        fetched: list[dict[str, Any]] = []
        for row in to_fetch:
            ids = json.loads(row.get("canonical_ids_json") or "{}")
            raw = ids.get("doi") or ids.get("arxiv_id")
            prior = self._latest_document(row["id"])
            if prior is not None and prior.get("access_status") in (
                    "retrieved_full_text", "retrieved_html_landing"):
                fetched.append({"paper_id": row["id"],
                                "status": "already_retrieved",
                                "document_id": prior["id"]})
                continue
            if raw is None:
                fetched.append({"paper_id": row["id"], "status": "unavailable",
                                "note": "no DOI or arXiv identifier"})
                continue
            if cfg is None:
                # No saved config snapshot -> the retrieval transports are
                # not configured; record unavailable rather than touch the
                # network (agent-driven mode, spec section 4).
                fetched.append({"paper_id": row["id"], "status": "unavailable",
                                "note": "no config snapshot; retrieval "
                                        "deferred to a configured run"})
                continue
            try:
                report = fetch_document(raw, cfg, dict(os.environ),
                                         out_dir=out_dir, cache=cache,
                                         store=self.store, run_id=self.run_id,
                                         paper_id=row["id"])
            except Exception as e:  # noqa: BLE001 -- a failed fetch is DATA
                fetched.append({"paper_id": row["id"], "status": "failed",
                                "error": f"{type(e).__name__}: {e}"})
                continue
            entry: dict[str, Any] = {"paper_id": row["id"], "status": "fetched",
                                    "access_level": report.get("access_level")}
            doc_row = self._latest_document(row["id"])
            if report.get("document") is not None and doc_row is not None:
                # Parse the downloaded file into the stable text index
                # (child-process isolated for PDFs, spec section 16).
                try:
                    from papercli.documents.ingest import parse_document_bytes
                    pr = parse_document_bytes(
                        Path(report["document"]["path"]).read_bytes())
                    grade = compute_parse_grade(pr)
                    eff = cap_document_access_level(
                        entry.get("access_level") or "full_text",
                        grade, has_extracted_text(pr))
                    n = self.store.store_document_text(
                        doc_row["id"], pr.get("text_index") or [])
                    self.store._conn.execute(
                        "UPDATE documents SET parse_quality=?, scanned_flag=?, "
                        "parse_warnings_json=?, access_level=? WHERE id=?",
                        (grade, int(bool(pr.get("scanned"))),
                         json.dumps(list(pr.get("warnings") or [])),
                         eff, doc_row["id"]))
                    entry.update({"document_id": doc_row["id"],
                                  "parse_grade": grade,
                                  "access_level": eff,
                                  "sections_stored": n})
                except Exception as e:  # noqa: BLE001 -- parse is an enhancement
                    entry["parse_error"] = f"{type(e).__name__}: {e}"
            fetched.append(entry)
        return {
            "cap": cap,
            "candidates": len(to_fetch),
            "fetched": len([f for f in fetched
                            if f.get("status") in ("fetched", "already_retrieved")]),
            "already_retrieved": len([f for f in fetched
                                      if f.get("status") == "already_retrieved"]),
            "unavailable": len([f for f in fetched
                                if f.get("status") in ("unavailable", "failed")]),
            "details": fetched,
        }

    def _task_evidence(self, p: dict[str, Any]) -> dict[str, Any]:
        """Deterministic evidence extraction (never-infer gate, spec 9)."""
        from papercli.research.evidence import extract_evidence_for_document
        results: list[dict[str, Any]] = []
        read_full = read_partial = 0
        for row in self.store.papers_for_run(self.run_id):
            doc = self._latest_document(row["id"])
            if doc is None:
                continue
            if doc.get("access_level") not in ("full_text", "partial_full_text"):
                continue
            if self.store._conn.execute(
                    "SELECT COUNT(*) c FROM document_text WHERE document_id=?",
                    (doc["id"],)).fetchone()["c"] == 0:
                continue
            res = extract_evidence_for_document(
                self.store, run_id=self.run_id,
                paper_id=row["id"], document_id=doc["id"])
            results.append(res)
            if doc.get("access_level") == "full_text":
                read_full += 1
            else:
                read_partial += 1
        return {
            "documents": len(results),
            "evidence_total": sum(r.get("count") or 0 for r in results),
            "papers_read": read_full + read_partial,
            "papers_read_partial": read_partial,
            "gated_papers": [r.get("paper_id") for r in results if r.get("gated")],
        }

    # -- the dispatch loop -----------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        """Run the DAG to a terminal closure state. Never raises."""
        self.build_dag()
        self._record_start_wall()
        self._heartbeat()
        self._log("worker_started", {
            "run_id": self.run_id,
            "protocol_revision": (self.store.run_row(self.run_id) or {})
            .get("protocol_revision"),
            "model_configured": self.model_cfg is not None,
        })
        idle_polls = 0
        while True:
            if self.cancel_requested():
                self._log("cancel_requested", {"ts": self._now_fn()})
                break
            if self.deadline_reached():
                self._deadline_reached = True
                self._log("deadline_reached", {"ts": self._now_fn()})
                break
            released = self.queue.release_expired(self.run_id)
            if released:
                self._log("leases_released", {"tasks": released})
            task = self._claim_one()
            if task is None:
                due = self._next_due_iso()
                if due is None:
                    break
                # A retry_wait task is deferred THROUGH SCHEDULER STATE
                # (spec section 12): poll until it is due. The poll is
                # bounded -- after the idle cap the run closes partial and
                # the deferred task is picked up by the next resume.
                idle_polls += 1
                if idle_polls > _MAX_IDLE_POLLS:
                    self._log("idle_cap_reached",
                               {"deferred_task_due_at": due})
                    break
                wait = min(self._seconds_until(due), 30.0)
                if wait > 0:
                    self._sleep(wait)
                if self.deadline_reached():
                    self._deadline_reached = True
                    break
                if self.cancel_requested():
                    break
                continue
            idle_polls = 0
            outcome = self._run_leased_task(task)
            if outcome == "done":
                self._checkpoint(after_task=task.task_id)
                self._heartbeat()
        self.runner.close()
        return self.close_bundle()

    def _claim_one(self) -> Any | None:
        claimed = self.queue.claim(self.run_id, "worker", limit=1)
        return claimed[0] if claimed else None

    def _record_start_wall(self) -> None:
        """The durable wall-clock start the deadline anchors to (spec 11)."""
        counters = json.loads(
            (self.store.run_row(self.run_id) or {}).get("counters_json") or "{}")
        ws = counters.setdefault("worker", {})
        if not ws.get("started_at_wall"):
            ws["started_at_wall"] = self._now_fn()
            self.store.update_run(self.run_id, counters_json=json.dumps(counters))

    def _next_due_iso(self) -> str | None:
        row = self.store._conn.execute(
            "SELECT MIN(next_attempt_at) d FROM tasks "
            "WHERE run_id=? AND state='retry_wait' AND next_attempt_at IS NOT NULL",
            (self.run_id,)).fetchone()
        return row["d"] if row else None

    def _seconds_until(self, iso: str) -> float:
        """Seconds until an ISO-8601 instant, against this worker's clock.

        Uses the injectable ``now_fn`` so tests control time; both sides
        are parsed (naive is treated as UTC)."""
        def _parse(v: str):
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        try:
            limit = _parse(iso)
            now = _parse(self._now_fn())
            return max(0.0, (limit - now).total_seconds())
        except (ValueError, TypeError):
            return 0.0

    def _run_leased_task(self, task: Any) -> str:
        self.queue.start(self.run_id, task.task_id, "worker")
        try:
            result = self._execute(task)
            if not isinstance(result, dict):
                result = {"note": str(result)}
            self.queue.complete(self.run_id, task.task_id, "worker",
                                result=result)
            self._log("task_completed", {
                "task_id": task.task_id, "type": task.type,
                "result_summary": self._summarize_result(task.type, result)})
            return "done"
        except TaskError as e:
            if e.result and e.result.get("status") == "retry_wait":
                # Bounded rate backoff through scheduler state (spec 12):
                # defer until next_attempt_at, never a storm.
                wait = min(float(e.result.get("wait_seconds") or 0.0), 30.0)
                self.queue.fail(self.run_id, task.task_id, "worker",
                                error=f"retry_wait: {e}",
                                retry_delay=max(wait, 1.0))
                self._log("task_deferred", {"task_id": task.task_id,
                                             "type": task.type,
                                             "wait_seconds": wait})
                return "done"
            return self._fail_task(task, str(e))
        except Exception as e:  # noqa: BLE001 -- a crash fails the task, not the run
            return self._fail_task(task, f"{type(e).__name__}: {e}")

    def _fail_task(self, task: Any, message: str) -> str:
        """Fail with jittered bounded backoff; the queue's max_attempts
        escalation (retry_wait -> failed) is the scheduler's own state."""
        delay = (self._retry_delay
                 + random.uniform(0.0, max(0.0, self._retry_jitter)))
        self.queue.fail(self.run_id, task.task_id, "worker",
                        error=message, retry_delay=delay)
        self._log("task_failed", {"task_id": task.task_id, "type": task.type,
                                   "error": message, "retry_delay": delay})
        return "done"

    @staticmethod
    def _summarize_result(task_type: str, result: Mapping[str, Any]) -> dict[str, Any]:
        keys = {
            "discover": ("record_count", "completeness", "deferred_records",
                         "errors"),
            "screen": ("decided", "uncertain", "unresolved", "links_created"),
            "expand": ("added", "deferred", "complete"),
            "retrieval": ("fetched", "already_retrieved", "unavailable"),
            "evidence": ("documents", "evidence_total", "papers_read"),
            "screen_uncertain": ("resolved", "unresolved"),
            "extract_findings": ("documents_processed", "findings_total"),
            "synthesize": ("claims_recorded", "model_call_outcome"),
            "critique": ("claims_audited",),
        }.get(task_type, ())
        out = {k: result.get(k) for k in keys if k in result}
        out["status"] = result.get("status")
        return out

    # -- closure (spec section 12) ------------------------------------------------------
    def close_bundle(self) -> dict[str, Any]:
        """Close the run: checkpoint, run summary, terminal state.

        On budget exhaustion the closure slice is RESERVED so a partial
        synthesis + status bundle are labeled, never silently missing
        (spec section 12)."""
        from papercli.exports.run_summary import build_run_summary, write_run_summary
        state = self._closure_state()
        self._checkpoint(after_task="close")
        self.store.update_run(self.run_id, state=state, ended_at=self._now_fn())
        summary = build_run_summary(self.store, self.run_id)
        if self.shared_budget.exhausted:
            self.shared_budget.reserve(purpose="status_bundle")
        summary_path = write_run_summary(self.store, self.run_id, summary,
                                         self.work_dir / "run-summary.json")
        self._heartbeat()
        self._log("run_closed", {"state": state,
                                 "summary": str(summary_path),
                                 "budget": self.shared_budget.to_dict()})
        return summary

    def _closure_state(self) -> str:
        if self._cancel_requested:
            return "cancelled"
        tasks = self.queue.list(self.run_id)
        unfinished = [t for t in tasks
                      if t.state in ("pending", "leased", "running",
                                     "retry_wait")]
        if unfinished:
            return "partial"
        if any(t.state == "failed" for t in tasks):
            return "failed"
        if self.shared_budget.exhausted:
            # The model budget ran out mid-DAG: a model task was skipped
            # or failed, so the run is a PARTIAL closure, not completed
            # (the reserved slice keeps the bundle labeled, spec 12).
            return "partial"
        return "completed"

    # -- checkpoint / heartbeat / log -------------------------------------------------------
    def _checkpoint(self, *, after_task: str) -> None:
        """Checkpoint after EVERY task (spec section 11): the single-writer
        snapshot of task states + the run budget under the run dir."""
        counters = json.loads(
            (self.store.run_row(self.run_id) or {}).get("counters_json") or "{}")
        tasks = self.queue.list(self.run_id)
        by_state: dict[str, int] = {}
        for t in tasks:
            by_state[t.state] = by_state.get(t.state, 0) + 1
        worker_state = counters.get("worker", {})
        worker_state.update({
            "deadline_at": self.wall_deadline(),
            "deadline_reached": self._deadline_reached,
            "budget": self.shared_budget.to_dict(),
            "budget_deficit": bool(self.runner.last_deficit),
            "last_checkpoint_task": after_task,
            "checkpointed_at": self._now_fn(),
            "tasks_by_state": by_state,
        })
        counters["worker"] = worker_state
        self.store.update_run(self.run_id, counters_json=json.dumps(counters))

    def _heartbeat(self) -> None:
        """Durable heartbeat: monotonic + WALL timestamps.

        Status uses the WALL timestamp to report age; a stale heartbeat
        means possibly-interrupted -- NEVER 'running' from a PID alone
        (spec section 12)."""
        hb = {
            "monotonic": self.clock(),
            "wall": time.time(),
            "wall_iso": self._now_fn(),
            "pid": os.getpid(),
            "ts": self._now_fn(),
        }
        self._write_json_atomic(self.work_dir / "heartbeat.json", hb)

    def _write_json_atomic(self, path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, default=str, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    def _log(self, event: str, data: Mapping[str, Any]) -> None:
        """One JSON envelope per line (spec section 6: batch/follow form)."""
        envelope = {
            "schema_version": "1.0",
            "status": "ok",
            "data": {"event": event, **dict(data),
                     "run_id": self.run_id, "ts": self._now_fn()},
            "warnings": [],
        }
        line = json.dumps(envelope, default=str, ensure_ascii=False)
        with self._log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
