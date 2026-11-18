"""Model-driven reasoning tasks (Phase 5).

Pure functions ``(store, task_payload) -> result`` registered as task
types on the Phase-4 TaskQueue; the detached worker (Phase 6) dispatches
them through the shared lease protocol.

* :func:`screen_uncertain` -- bounded model recheck of the run's
  uncertainty queue (spec section 9);
* :func:`extract_findings` -- model-driven evidence extraction for
  usable-access documents only (the never-infer gate, spec section 9);
* :func:`synthesize` -- theme-organized draft with registry-only
  citations (spec section 10);
* :func:`critique` -- claim audit + bounded revision (spec section 10).
"""

from papercli.reasoning.common import (
    TASK_TYPES,
    PROMPT_VERSIONS,
    build_adapter,
    model_available,
    run_protocol,
    input_hash,
    skipped_result,
    bibliography_for_run,
    citation_key,
)
from papercli.reasoning.screening import screen_uncertain
from papercli.reasoning.extraction import extract_findings
from papercli.reasoning.synthesis import synthesize
from papercli.reasoning.critique import critique

#: task type -> implementation (the Phase-6 dispatch table).
TASKS: dict = {
    "screen_uncertain": screen_uncertain,
    "extract_findings": extract_findings,
    "synthesize": synthesize,
    "critique": critique,
}

__all__ = [
    "TASK_TYPES",
    "PROMPT_VERSIONS",
    "TASKS",
    "screen_uncertain",
    "extract_findings",
    "synthesize",
    "critique",
    "build_adapter",
    "model_available",
    "run_protocol",
    "input_hash",
    "skipped_result",
    "bibliography_for_run",
    "citation_key",
]
