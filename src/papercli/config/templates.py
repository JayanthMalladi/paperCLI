"""Templates written by ``papers init``.

``papers init`` creates ``.papercli/config.toml`` and a research brief
template in the project. Secrets in the template are placeholder
environment-variable references -- never literal values.
"""

from __future__ import annotations

__all__ = [
    "CONFIG_TEMPLATE",
    "BRIEF_TEMPLATE",
    "ENV_TEMPLATE",
    "readme_init_note",
]

# Matches the spec section 13 toml example. Every secret is an env reference.
CONFIG_TEMPLATE = """# PaperCLI project config (.papercli/config.toml)
#
# Secrets are NOT stored here. They reference environment variables whose
# values live in the environment or in a local, git-ignored .env file.
# Precedence: CLI args > environment variables > this file > user config > defaults.

[contact]
# Required by Unpaywall and used as the polite-pool identifier for Crossref.
# Enter your real email, then set PAPERS_EMAIL to the same value (see .env).
email = "REPLACE_WITH_REAL_EMAIL"

[providers.unpaywall]
enabled = true
email_env = "PAPERS_EMAIL"

[providers.crossref]
enabled = true

[providers.openalex]
enabled = true
api_key_env = "OPENALEX_API_KEY"

[providers.arxiv]
enabled = true

[model]
# One tested OpenAI-compatible chat-completions adapter. The endpoint is
# visible here so it is always clear which reasoning service is selected.
# The value returned by your endpoint is accepted but NOT enforced for
# structured output -- the adapter parses JSON leniently and re-validates.
adapter = "openai_compatible"
base_url = "REPLACE_WITH_SELECTED_ENDPOINT"
model = "REPLACE_WITH_TESTED_MODEL_ID"
api_key_env = "PAPERS_LLM_API_KEY"

[execution]
network_workers = 4
model_workers = 2
request_timeout_seconds = 30
max_response_mb = 50
"""

# Local, git-ignored example. Values stay out of version control.
ENV_TEMPLATE = """# Local secrets. This file is git-ignored (.env). Never commit real values.
#
# Unpaywall contact email (polite pool for Crossref also uses this).
PAPERS_EMAIL=your.real@example.org

# OpenAlex activated account key (USD-credit model; required to cursor-page
# past the first page and to run the configured overnight discovery profile).
OPENALEX_API_KEY=your_openalex_key

# OpenAI-compatible model endpoint.
PAPERS_LLM_API_KEY=your_llm_api_key
PAPERS_LLM_BASE_URL=https://your.selected.endpoint
"""

# Research brief matching spec section 7. Budget guardrails are the
# documented example ceilings, not promised throughput.
BRIEF_TEMPLATE = """schema_version: '1.0'
question: 'REPLACE WITH RESEARCH QUESTION'
purpose: 'Thesis background and research-gap identification'
review_type: narrative
date_range: {from: null, to: null}
languages: [en]
include_preprints: true
seed_identifiers: []
inclusion_criteria: []
exclusion_criteria: []
themes_to_investigate: []
outputs: [md, pdf, docx, bib, csl, csv]
citation_style: ieee
limits:
  wall_minutes: 360
  max_search_queries: 30
  max_discovered_records: 1000
  max_full_text_papers: 60
  max_model_calls: 300
  max_input_tokens: 1000000
  max_output_tokens: 150000
  max_estimated_cost_usd: null
"""


def readme_init_note() -> str:
    """Short, honest note printed after init about next steps."""
    return (
        "Next: (1) fill in .papercli/config.toml contact.email, (2) copy the "
        "shown .env example into a local .env with real values (git-ignored), "
        "then (3) run: papers doctor --json"
    )
