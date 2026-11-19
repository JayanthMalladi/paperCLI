"""Explicit run recovery (Phase 6, spec sections 5/11/12).

``resume_run`` is the user-facing recovery path for an interrupted run:

* detect the stale state -- a dead per-run lock holder and/or a heartbeat
  older than :data:`detach.STALE_AFTER_SECONDS` (spec section 12: NEVER
  trust a PID alone);
* reclaim the stale lock and release every EXPIRED task lease
  (:meth:`TaskQueue.release_expired`) so the crashed worker's in-flight
  work is re-claimable;
* re-dispatch ONLY the unfinished tasks: completed task rows are
  untouched and their result hashes stay byte-identical (idempotent per
  spec section 11);
* re-launch the worker through :mod:`papercli.jobs.detach` so the same
  durable handshake/heartbeat/lock protocol continues;
* if the brief/config the user is now passing does not match the run's
  saved ``protocol_hash``, do NOT silently resume: a changed brief or
  configuration is a NEW revision (spec section 5) -- raise a typed
  error the CLI maps to exit 2 with guidance, never mutate the prior
  evidence.

There is no automatic boot-time service install (spec section 4):
recovery is always an explicit ``papers review resume``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from papercli.jobs import detach
from papercli.jobs.tasks import TaskQueue
from papercli.storage.db import PaperStore

__all__ = [
    "BriefChangedError",
    "ResumeResult",
    "brief_hash",
    "saved_brief_ref",
    "resume_run",
]

#: Re-exported so callers/tests have one import surface (detach owns the
#: semantics).
BriefChangedError = detach.BriefChangedError


@dataclass
class ResumeResult:
    """What ``resume_run`` hands back."""

    run_id: str
    state: str
    resumed: bool
    launched: bool
    pid: int | None
    tasks_released: int
    tasks_requeued: int
    launch: Any = None
    notes: list[str] = field(default_factory=list)


def _requeue_unfinished(store: PaperStore, run_id: str) -> tuple[int, int]:
    """Re-queue the unfinished tasks of a crashed worker.

    Interrupted work loses its in-flight state (a ``running``/``leased``
    task may be past its lease). Only states that are NOT terminal get
    touched; ``succeeded``/``skipped`` rows and their result hashes are
    never mutated (spec section 11).

    Returns ``(released, requeued)`` counts.
    """
    queue = TaskQueue(store)
    released = queue.release_expired(run_id)
    # A crash can also leave a task in 'running'/'leased' whose lease is
    # NOT yet expired (TTL still in the future): re-queue those too --
    # their owner is dead, so holding the lease would strand the task.
    rows = store._conn.execute(
        "SELECT id FROM tasks WHERE run_id=? "
        "AND state IN ('leased','running')",
        (run_id,)).fetchall()
    requeued = 0
    for row in rows:
        with store._conn:
            cur = store._conn.execute(
                "UPDATE tasks SET state='pending', lease_owner=NULL, "
                "lease_expires_at=NULL WHERE id=? AND state IN "
                "('leased','running')",
                (row["id"],))
            if cur.rowcount:
                requeued += 1
    return released, requeued


def brief_hash(brief: Mapping[str, Any]) -> str:
    """The brief digest, computed EXACTLY like
    :mod:`papercli.research.plan` saves it on the run (``brief_ref``
    ``sha256``): a sorted-keys JSON digest truncated to 16 hex chars.

    Resume compares the CURRENT brief's digest against the digest the run
    SAVED: a changed brief/config is a new revision, never a silent
    mutation of prior evidence (spec section 5).
    """
    import hashlib

    return hashlib.sha256(
        json.dumps(dict(brief), sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()[:16]


def saved_brief_ref(store: PaperStore,
                   run_id: str) -> dict[str, Any] | None:
    """The run's saved ``brief_ref`` (path + sha256), if the plan has one."""
    row = store.run_row(run_id) or {}
    plan = json.loads(row.get("plan_json") or "{}")
    ref = plan.get("brief_ref")
    return dict(ref) if isinstance(ref, Mapping) else None


def _check_brief_unchanged(store: PaperStore, run_id: str,
                           brief: Mapping[str, Any]) -> list[str]:
    """The resume preflight (spec section 5). Returns warning notes.

    When the run SAVED a ``brief_ref`` and the current brief's digest
    differs -> :class:`BriefChangedError` (the CLI maps it to exit 2 with
    new-revision guidance). When no digest was saved the check is
    impossible; that is surfaced as a warning, not a silent pass.
    """
    notes: list[str] = []
    ref = saved_brief_ref(store, run_id)
    if ref is None:
        notes.append("the run saved no brief hash; the changed-brief "
                     "check could not run (resume proceeds on the saved "
                     "protocol)")
        return notes
    current = brief_hash(brief)
    if current != str(ref.get("sha256") or ""):
        raise BriefChangedError(
            f"the brief no longer matches run {run_id} (the saved "
            f"brief_ref hash {str(ref.get('sha256'))} != the current "
            f"brief hash {current}); a changed brief or configuration "
            f"requires a NEW run revision -- start one with "
            f"`papers review plan` and launch it with `papers review "
            f"run`. The prior evidence is never silently mutated "
            f"(spec section 5).",
            {"run_id": run_id, "saved_brief_hash": ref.get("sha256"),
             "current_brief_hash": current,
             "protocol_hash": (store.run_row(run_id) or {}).get("protocol_hash"),
             "guidance": "start a new revision with `papers review "
                         "plan`"})
    return notes


def resume_run(
    store: PaperStore,
    run_id: str,
    *,
    brief: Mapping[str, Any] | None = None,
    cfg: Any = None,
    detach_flag: bool = True,
    detach_timeout_seconds: float = 30.0,
    heartbeat_interval: float = detach.DEFAULT_HEARTBEAT_INTERVAL,
    now_fn: Callable[[], Any] | None = None,
    child_fn: Callable[..., Any] | None = None,
) -> ResumeResult:
    """The explicit-recovery path (spec section 5: resume the SAME run; a
    changed brief/config is a NEW revision).

    ``brief`` (when given) is the user's current research brief: if its
    hash no longer matches the run's saved ``protocol_hash`` this raises
    :class:`BriefChangedError` (the CLI maps it to exit 2 with
    new-revision guidance) INSTEAD of silently mutating prior evidence.

    Terminal-state runs are NOT relaunched: a clean completed run is a
    no-op resume that reports "completed"; a partial/cancelled/failed run
    reports its state with guidance (a changed brief requires a new
    revision, spec section 5).
    """
    row = store.run_row(run_id)
    if row is None:
        raise detach.RunNotStartable(
            f"run not found: {run_id}", {"run_id": run_id})

    # -- changed-brief preflight (spec 5): no silent mutation -----------
    notes: list[str] = []
    if brief is not None:
        notes.extend(_check_brief_unchanged(store, run_id, brief))

    # -- terminal state: a clean completed run is a NO-OP resume -------
    if str(row.get("state") or "") in detach.TERMINAL_STATES:
        state = str(row.get("state"))
        if state == "completed":
            note = ("run is completed; resume is a no-op (a changed "
                    "brief requires a new revision, spec section 5)")
        else:
            note = (f"run ended in terminal state '{state}'; resume "
                    f"re-launches nothing (a changed brief requires a "
                    f"new revision, spec section 5)")
        return ResumeResult(
            run_id=run_id, state=state,
            resumed=False, launched=False, pid=None,
            tasks_released=0, tasks_requeued=0,
            notes=[note])

    # -- stale detection (spec 12): dead holder or old heartbeat -------
    info = detach.lock_held(store, run_id, now_fn=now_fn)
    stale = bool(info.get("stale"))
    if stale:
        notes.append("stale state detected: "
                     + (info.get("reason") or "no lock held"))

    # -- reclaim: stale lock + expired leases (idempotent, spec 11) ----
    released, requeued = _requeue_unfinished(store, run_id)
    if released:
        notes.append(f"released {released} expired task lease(s)")
    if requeued:
        notes.append(f"re-queued {requeued} in-flight task(s) from the "
                     f"crashed worker")

    # -- re-launch through the durable handshake/lock protocol ---------
    launch = detach.launch_worker(
        store, run_id, cfg=cfg, detach=detach_flag,
        detach_timeout_seconds=detach_timeout_seconds,
        heartbeat_interval=heartbeat_interval, now_fn=now_fn,
        child_fn=child_fn)
    state = str((store.run_row(run_id) or {}).get("state")
                or row.get("state"))
    return ResumeResult(
        run_id=run_id, state=state, resumed=stale or requeued > 0,
        launched=True, pid=launch.pid,
        tasks_released=released, tasks_requeued=requeued,
        launch=launch, notes=notes)
