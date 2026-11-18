"""Research pipeline: protocol, screening, extraction, synthesis (Phase 4).

Phase 4 ships the deterministic research layer:

* :mod:`papercli.research.plan` -- brief validation, deterministic
  database-specific query expansion, resource estimates, protocol hash +
  the planned Run row (the model-driven plan refinement is Phase 5);
* :mod:`papercli.research.screening` -- title/abstract + full-text
  screening decisions (spec section 9 field set), the bounded
  uncertainty-queue recheck, and identifier-first deduplication with
  link-not-merge provenance;
* :mod:`papercli.research.evidence` -- structured evidence extraction
  from parsed documents with the never-infer access-level gate;
* :mod:`papercli.research.expansion` -- bounded citation search expansion
  with provenance chains;
* :mod:`papercli.research.quality` -- the parse-quality grade + capped
  access level (D-1/D-3 fix);
* :mod:`papercli.research.tasks` -- the CLI-facing task protocol surface
  (D-2 fix).

The model-calling layer (findings, synthesis, claim ledger) is Phase 5;
the detached worker / run states surface is Phase 6.
"""

from papercli.research.expansion import expand_search
from papercli.research.evidence import (
    AccessLevelGate,
    classify_claim_type,
    extract_evidence_for_document,
    extract_evidence_for_run,
)
from papercli.research.plan import BriefError, build_protocol, plan_review
from papercli.research.quality import (
    cap_document_access_level,
    compute_parse_grade,
)
from papercli.research.screening import ScreeningPipeline, screening_csv_rows
from papercli.research.tasks import task_next, task_submit

__all__ = [
    "BriefError",
    "build_protocol",
    "plan_review",
    "expand_search",
    "AccessLevelGate",
    "classify_claim_type",
    "extract_evidence_for_document",
    "extract_evidence_for_run",
    "ScreeningPipeline",
    "screening_csv_rows",
    "task_next",
    "task_submit",
    "compute_parse_grade",
    "cap_document_access_level",
]
