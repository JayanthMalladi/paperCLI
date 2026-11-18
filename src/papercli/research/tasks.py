"""Task protocol surface (Phase 4, spec section 6) -- CLI wiring for the
lease-based task queue (D-2 fix).

``papers task next --run RUN_ID --worker AGENT_ID``:
leases the next claimable task for the worker and emits the JSON envelope
with the task payload + lease + expiry. No task -> ``status ok`` with
``task: null``.

``papers task submit TASK_ID --input result.json``:
validates the submission against the task queue protocol:
reject invalid / stale / duplicated / source-inconsistent submissions
with distinct codes; an idempotent repeated submission with the same
payload hash returns the prior result.

Both are driven through the Phase-3 :class:`TaskQueue` + :func:`submit`
library protocol -- the detached worker (Phase 6) and external agents
use the same lease.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from papercli.jobs.tasks import TaskQueue, SubmissionOutcome, submit
from papercli.storage.db import PaperStore

__all__ = [
    "task_next",
    "task_submit",
    "TASK_SUBMIT_CODES",
]

#: Distinct submission outcome codes (spec section 6).
TASK_SUBMIT_CODES = {
    "accepted": SubmissionOutcome.ACCEPTED,
    "duplicate": SubmissionOutcome.DUPLICATE,
    "rejected_invalid": SubmissionOutcome.REJECTED_INVALID,
    "rejected_stale": SubmissionOutcome.REJECTED_STALE,
    "rejected_source": SubmissionOutcome.REJECTED_SOURCE,
    "rejected_state": SubmissionOutcome.REJECTED_STATE,
}


def task_next(
    store: PaperStore,
    run_id: str,
    worker: str,
    *,
    lease_ttl: int | float = 300,
) -> dict[str, Any]:
    """Lease the next claimable task for ``worker`` (envelope body).

    ``data.task`` is the leased task with its payload + lease + expiry,
    or ``null`` when nothing is claimable (which is still an ``ok``
    envelope -- a queue with no pending work is not an error).
    """
    q = TaskQueue(store)
    # Recover tasks whose leases expired (crashed worker).
    q.release_expired(run_id)
    claimed = q.claim(run_id, worker, lease_ttl=lease_ttl, limit=1)
    if not claimed:
        return {
            "run_id": run_id,
            "worker": worker,
            "task": None,
            "pending": len(q.list(run_id, state="pending")),
        }
    task = claimed[0]
    payload = dict(task.payload or {})
    payload.setdefault("input_hash", task.input_hash)
    return {
        "run_id": run_id,
        "worker": worker,
        "task": {
            "task_id": task.task_id,
            "type": task.type,
            "state": task.state,
            "attempt": task.attempt,
            "dependencies": task.dependencies,
            "payload": payload,
            "lease": {
                "owner": task.lease_owner,
                "expires_at": task.lease_expires_at,
            },
        },
        "pending": max(0, len(q.list(run_id, state="pending")) - 1),
    }


def task_submit(
    store: PaperStore,
    run_id: str,
    task_id: str,
    worker: str,
    payload: Mapping[str, Any],
    *,
    source_input_hash: str | None = None,
) -> dict[str, Any]:
    """Submit a task result (envelope body); see :func:`submit` semantics."""
    q = TaskQueue(store)
    result = submit(q, run_id, task_id, worker, payload,
                    source_input_hash=source_input_hash)
    return {
        "run_id": run_id,
        "task_id": task_id,
        "worker": worker,
        **result,
    }


def load_submission_input(path: Path | str) -> dict[str, Any]:
    """Read the ``--input result.json`` payload file (invalid -> error)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"input file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"input file is not valid JSON: {p} ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"submission payload must be a JSON object: {p}")
    return data
