"""Task layer: lease-based queue shared by standalone workers and agents.

The detached overnight worker (Phase 6) and the external-agent protocol
(``papers task next/submit``) both drive the same lease protocol here.
"""

from papercli.jobs.tasks import (
    TaskQueue,
    TaskQueueError,
    TaskState,
    TASK_STATES,
    DEFAULT_LEASE_TTL_SECONDS,
    lease_expires,
    payload_hash_of,
    submit,
    SubmissionOutcome,
)
from papercli.jobs.worker import (
    DAG_VERSION,
    TASK_TYPES,
    TaskError,
    ReasoningTaskRunner,
    RunWorker,
)

__all__ = [
    "TaskQueue",
    "TaskQueueError",
    "TaskState",
    "TASK_STATES",
    "DEFAULT_LEASE_TTL_SECONDS",
    "lease_expires",
    "payload_hash_of",
    "submit",
    "SubmissionOutcome",
    "DAG_VERSION",
    "TASK_TYPES",
    "TaskError",
    "ReasoningTaskRunner",
    "RunWorker",
]
