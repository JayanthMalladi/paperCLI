r"""Lease-based task queue shared by standalone workers and external agents.

Spec section 6 (``papers task next/submit``) and section 11 (task states,
lease expiry, idempotency keys). This is the *task layer* only: the queue
protocol. The detached overnight worker (Phase 6) and the external-agent
protocol both drive the same lease here.

State machine (spec section 11)::

    pending -> leased -> running -> succeeded
                       \-> failed
    retry_wait -> pending (after next_attempt_at) -> ...
    skipped (terminal, dependency or upstream failure)

Lease lifecycle (a lost worker is recovered, never wedged):

    claim:    pending / due retry_wait -> leased (owner + expiry set)
    start:    leased -> running
    complete: leased/running -> succeeded (result hash recorded)
    fail:     leased/running -> retry_wait (attempts left) | failed
    re-lease: an EXPIRED lease (lease_expires_at < now) is released back
               to pending so another worker can claim it. This recovers a
               crashed or vanished worker without any live signal.

Idempotency (spec section 11): a task is keyed by
``(run_id, task type, input/content hash, protocol revision)``.
Re-enqueueing the same key inside a run returns the prior task row, and a
repeated submission with the same payload hash returns the prior result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "TaskQueue",
    "TaskState",
    "TaskQueueError",
    "TASK_STATES",
    "DEFAULT_LEASE_TTL_SECONDS",
    "lease_expires",
    "submit",
    "payload_hash_of",
    "SubmissionOutcome",
]

#: Valid task states (spec section 11).
TASK_STATES = (
    "pending", "leased", "running", "succeeded", "retry_wait", "failed", "skipped",
)

#: Terminal states: no further transitions.
TERMINAL_STATES = ("succeeded", "failed", "skipped")

#: Default lease TTL (seconds) for a claimed task. A worker that loses its
#: process (crash, detach) stops renewing; after the TTL the lease is
#: reclaimable by another worker (spec section 11: "leases expire after
#: worker loss and allow recovery").
DEFAULT_LEASE_TTL_SECONDS = 300


def _now_iso() -> str:
    return utc_now_iso()


def lease_expires(ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS) -> str:
    """UTC ISO-8601 timestamp for a lease expiring ``ttl_seconds`` from now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="milliseconds")


class TaskState:
    """Readable state constants (match the spec strings exactly)."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALL = TASK_STATES


@dataclass
class Task:
    """One durable unit of work (mirrors the ``tasks`` table + task schema)."""

    task_id: str
    run_id: str
    type: str
    state: str
    attempt: int = 0
    input_hash: str | None = None
    result_hash: str | None = None
    dependencies: list[str] | None = None
    payload: Mapping[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "type": self.type,
            "state": self.state,
            "attempt": self.attempt,
            "input_hash": self.input_hash,
            "result_hash": self.result_hash,
            "dependencies": list(self.dependencies or []),
        }
        if self.lease_owner or self.lease_expires_at:
            d["lease"] = {"owner": self.lease_owner,
                         "expires_at": self.lease_expires_at}
        if self.last_error:
            d["last_error"] = self.last_error
        return d


class TaskQueue:
    """Lease-based claim protocol over the shared single-writer store.

    All operations are transactional on the one ``PaperStore`` connection
    (spec section 11 single-writer strategy).
    """

    def __init__(self, store: PaperStore):
        self._store = store
        self._conn = store.connection

    # -- idempotency key (spec section 11) --------------------------------
    @staticmethod
    def idempotency_key(task_type: str, input_hash: str,
                        protocol_revision: str = "1") -> str:
        """``task type + input/content hash + protocol revision``."""
        return f"{task_type}:{input_hash}:{protocol_revision}"

    # -- enqueue (idempotent) ---------------------------------------------
    def enqueue(
        self,
        run_id: str,
        task_type: str,
        input_hash: str,
        *,
        payload: Mapping[str, Any] | None = None,
        dependencies: list[str] | None = None,
        protocol_revision: str = "1",
        max_attempts: int = 3,
    ) -> Task:
        """Enqueue a task; a duplicate idempotency key returns the existing row."""
        key = self.idempotency_key(task_type, input_hash, protocol_revision)
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND idempotency_key = ?",
            (run_id, key),
        ).fetchone()
        if row is not None:
            return self._to_task(row)

        task_id = PaperStore.new_id("task")
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks (id, run_id, type, input_hash, state, attempt, "
                "idempotency_key, payload_json, protocol_revision, max_attempts, "
                "created_at, updated_at, dependencies_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, run_id, task_type, input_hash, "pending", 0,
                 key, json.dumps(dict(payload or {}), sort_keys=True),
                 protocol_revision, max_attempts, now, now,
                 json.dumps(list(dependencies or []))),
            )
        return Task(task_id=task_id, run_id=run_id, type=task_type,
                    state="pending", input_hash=input_hash,
                    dependencies=list(dependencies or []), payload=payload)

    # -- claim (lease protocol) --------------------------------------------
    def claim(self, run_id: str, worker: str, *,
              lease_ttl: int | float = DEFAULT_LEASE_TTL_SECONDS,
              limit: int = 1) -> list[Task]:
        """Claim up to ``limit`` tasks for ``worker``.

        Claimable tasks are ``pending``, or ``retry_wait`` whose
        ``next_attempt_at`` is due, and whose dependencies have all
        *succeeded*. Each claim moves the task to ``leased`` with the
        worker's lease owner + expiry stamped atomically.
        """
        now = _now_iso()
        expires = lease_expires(lease_ttl)
        claimed: list[Task] = []
        while len(claimed) < limit:
            row = self._conn.execute(
                """
                SELECT t.id FROM tasks t
                WHERE t.run_id = ?
                  AND ((t.state = 'pending')
                       OR (t.state = 'retry_wait'
                           AND t.next_attempt_at IS NOT NULL
                           AND t.next_attempt_at <= ?))
                  AND NOT EXISTS (
                    SELECT 1 FROM json_each(t.dependencies_json) d
                    JOIN tasks dep ON dep.id = d.value
                    WHERE dep.state != 'succeeded')
                ORDER BY t.created_at
                LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
            if row is None:
                break
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE tasks SET state='leased', lease_owner=?, "
                    "lease_expires_at=?, updated_at=? "
                    "WHERE id=? AND state IN ('pending','retry_wait')",
                    (worker, expires, now, row["id"]),
                )
                if cur.rowcount != 1:
                    # Lost a race to another claimant; re-scan.
                    continue
            claimed.append(self.get(run_id, row["id"]))
        return claimed

    # -- leased -> running ---------------------------------------------------
    def start(self, run_id: str, task_id: str, worker: str) -> Task:
        row = self._conn.execute(
            "SELECT state, lease_owner FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found in run {run_id}")
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET state='running', updated_at=? WHERE "
                "id=? AND state='leased' AND lease_owner=?",
                (_now_iso(), task_id, worker),
            )
            if cur.rowcount != 1:
                raise TaskQueueError(
                    f"task {task_id} is not leased to worker {worker} "
                    f"(state={row['state']}, owner={row['lease_owner']})")
        return self.get(run_id, task_id)

    # -- complete / fail -------------------------------------------------------
    def complete(self, run_id: str, task_id: str, worker: str,
                 result_hash: str | None = None,
                 result: Mapping[str, Any] | None = None) -> Task:
        """Mark a leased/running task succeeded; record its result hash."""
        from papercli.providers.cache import sha256_of
        if result is not None and result_hash is None:
            result_hash = sha256_of(
                json.dumps(dict(result), sort_keys=True).encode("utf-8"))
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET state='succeeded', result_hash=?, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE id=? AND state IN ('leased','running') AND lease_owner=?",
                (result_hash, _now_iso(), task_id, worker),
            )
            if cur.rowcount != 1:
                raise TaskQueueError(
                    f"task {task_id} is not in a completable state for worker {worker}")
        return self.get(run_id, task_id)

    def fail(self, run_id: str, task_id: str, worker: str, *,
             error: str | None = None, retry_delay: int | float = 0) -> Task:
        """Fail a task; back into ``retry_wait`` while attempts remain."""
        row = self._conn.execute(
            "SELECT attempt, max_attempts FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        attempt = (row["attempt"] or 0) + 1
        max_attempts = row["max_attempts"] or 3
        now = _now_iso()
        if attempt < max_attempts:
            with self._conn:
                self._conn.execute(
                    "UPDATE tasks SET state='retry_wait', attempt=?, "
                    "next_attempt_at=?, last_error=?, lease_owner=NULL, "
                    "lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (attempt, lease_expires(retry_delay), error, now, task_id),
                )
        else:
            with self._conn:
                self._conn.execute(
                    "UPDATE tasks SET state='failed', attempt=?, last_error=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                    (attempt, error, now, task_id),
                )
        return self.get(run_id, task_id)

    # -- re-lease on expiry (recover lost workers) ---------------------------
    def release_expired(self, run_id: str) -> int:
        """Return expired ``leased``/``running`` tasks to ``pending``.

        A worker that crashed stops renewing its lease; after the TTL the
        task is reclaimable. Returns the number of tasks released.
        """
        now = _now_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET state='pending', lease_owner=NULL, "
                "lease_expires_at=NULL, updated_at=? "
                "WHERE run_id=? AND state IN ('leased','running') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (now, run_id, now),
            )
            return cur.rowcount

    # -- skip (terminal) ---------------------------------------------------------
    def skip(self, run_id: str, task_id: str, reason: str | None = None) -> Task:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET state='skipped', last_error=?, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (reason, _now_iso(), task_id),
            )
            if cur.rowcount != 1:
                raise TaskQueueError(f"task {task_id} not skippable")
        return self.get(run_id, task_id)

    # -- reads -----------------------------------------------------------------
    def get(self, run_id: str, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND run_id = ?",
            (task_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found in run {run_id}")
        return self._to_task(row)

    def list(self, run_id: str, state: str | None = None) -> list[Task]:
        if state is not None:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE run_id=? AND state=? "
                "ORDER BY created_at",
                (run_id, state),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [self._to_task(r) for r in rows]

    def _to_task(self, row) -> Task:
        return Task(
            task_id=row["id"], run_id=row["run_id"], type=row["type"],
            state=row["state"], attempt=row["attempt"],
            input_hash=row["input_hash"], result_hash=row["result_hash"],
            dependencies=json.loads(row["dependencies_json"] or "[]"),
            payload=json.loads(row["payload_json"] or "{}"),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
        )


class SubmissionOutcome:
    """Result of a task submission (spec section 6 protocol semantics)."""

    ACCEPTED = "accepted"            #: payload recorded, task completed
    DUPLICATE = "duplicate"         #: same payload hash resubmitted; prior result returned
    REJECTED_INVALID = "rejected_invalid"   #: bad payload or unknown task
    REJECTED_STALE = "rejected_stale"        #: lease expired / not owned by worker
    REJECTED_SOURCE = "rejected_source"      #: payload hash inconsistent with task input
    REJECTED_STATE = "rejected_state"        #: task not in a submittable state


class TaskQueueError(Exception):
    """A task transition that the state machine forbids."""


def payload_hash_of(payload: Mapping[str, Any]) -> str:
    """Canonical-JSON SHA-256 of a submission payload (idempotency hash)."""
    from papercli.providers.cache import sha256_of
    return sha256_of(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8"))


def submit(queue: TaskQueue, run_id: str, task_id: str, worker: str,
           payload: Mapping[str, Any], *,
           source_input_hash: str | None = None) -> dict[str, Any]:
    """Record a worker submission against the task-queue protocol.

    Semantics (spec section 6): reject invalid, stale, duplicated, or
    source-inconsistent submissions explicitly. An idempotent repeated
    submission with the same payload hash returns the prior result.
    """
    task = None
    try:
        task = queue.get(run_id, task_id)
    except KeyError:
        return {
            "status": SubmissionOutcome.REJECTED_INVALID,
            "reason": f"unknown task {task_id} in run {run_id}",
            "task_id": task_id,
        }
    phash = payload_hash_of(payload)
    now = _now_iso()
    conn = queue._conn

    # Duplicate guard (idempotency, spec section 6): the SAME payload
    # already ACCEPTED by this task returns the prior result. Only accepted
    # rows carry a result; a REJECTED submission (stale / invalid /
    # source-mismatch) produced no result and must NOT shadow a fresh claim.
    # Otherwise a different worker that reclaims a recovered task and
    # resubmits the identical payload would be wrongly deduped against the
    # earlier rejection instead of accepted.
    prior = conn.execute(
        "SELECT * FROM task_submissions WHERE task_id=? AND payload_hash=? "
        "AND status=?",
        (task_id, phash, SubmissionOutcome.ACCEPTED),
    ).fetchone()
    if prior is not None:
        prev = json.loads(prior["result_json"] or "{}")
        return {
            "status": SubmissionOutcome.DUPLICATE,
            "reason": "same payload hash already accepted; prior result returned",
            "prior_result": prev,
            "payload_hash": phash,
        }

    def _record_rejected(status: str, reason: str) -> dict[str, Any]:
        sub_id = PaperStore.new_id("sub")
        with conn:
            conn.execute(
                "INSERT INTO task_submissions (id, task_id, run_id, worker, "
                "payload_hash, status, rejection_reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (sub_id, task_id, run_id, worker, phash, status, reason),
            )
        return {"status": status, "reason": reason, "payload_hash": phash}

    # Source-consistency: when the task's input hash is supplied, the payload
    # must carry it verbatim (a mismatch is rejected, not silently accepted).
    if source_input_hash is not None:
        declared = (payload or {}).get("input_hash")
        if declared and declared != source_input_hash:
            return _record_rejected(
                SubmissionOutcome.REJECTED_SOURCE,
                f"payload input_hash {declared!r} != task input_hash "
                f"{source_input_hash!r}",
            )

    # State / lease checks.
    if task.state in TERMINAL_STATES:
        if task.state == "succeeded" and task.result_hash == phash:
            return {
                "status": SubmissionOutcome.DUPLICATE,
                "reason": "task already succeeded with this exact payload hash",
                "prior_result": {"result_hash": task.result_hash},
                "payload_hash": phash,
            }
        return _record_rejected(
            SubmissionOutcome.REJECTED_STATE,
            f"task in terminal state {task.state!r}")
    if task.state not in ("leased", "running"):
        return _record_rejected(
            SubmissionOutcome.REJECTED_STATE,
            f"task in state {task.state!r}; only leased/running accept submissions")
    # Lease ownership: a submission must come from the owning worker.
    if task.lease_owner is not None and task.lease_owner != worker:
        return _record_rejected(
            SubmissionOutcome.REJECTED_STALE,
            f"lease owned by worker {task.lease_owner!r}, not {worker!r}")
    # Expired lease = stale (the task is reclaimable by another worker).
    if task.lease_expires_at is not None and task.lease_expires_at < now:
        return _record_rejected(
            SubmissionOutcome.REJECTED_STALE,
            f"lease expired at {task.lease_expires_at}; task is reclaimable")

    # Accept: complete the task with this payload hash, record the submission.
    queue.complete(run_id, task_id, worker, result_hash=phash, result=payload)
    sub_id = PaperStore.new_id("sub")
    with conn:
        # The 0002 UNIQUE index on (task_id, payload_hash) spans all statuses,
        # so a REJECTED audit row for this payload would collide with the
        # accepted insert. A rejected submission is not a result and never
        # shadows a later acceptance by a reclaiming worker, so clear any
        # non-accepted rows for this payload before recording the accepted one.
        conn.execute(
            "DELETE FROM task_submissions WHERE task_id=? AND payload_hash=? "
            "AND status <> ?",
            (task_id, phash, SubmissionOutcome.ACCEPTED),
        )
        conn.execute(
            "INSERT INTO task_submissions (id, task_id, run_id, worker, "
            "payload_hash, status, result_hash, result_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sub_id, task_id, run_id, worker, phash,
             SubmissionOutcome.ACCEPTED, phash,
             json.dumps(dict(payload), sort_keys=True)),
        )
    return {
        "status": SubmissionOutcome.ACCEPTED,
        "task_id": task_id,
        "payload_hash": phash,
        "result": {"task_id": task_id, "result_hash": phash},
    }
