"""Published machine contract: JSON Schemas for every agent artifact.

Schemas are versioned **independently** of the package version. See
``papercli.agent_contract.schemas`` and :func:`papercli.agent_contract.schemas.validate`.
"""

from papercli.agent_contract.schemas import (
    SCHEMA_VERSION,
    schema_names,
    load_schema,
    validate,
    validate_envelope,
    SCHEMAS_DIR,
)

__all__ = [
    "SCHEMA_VERSION",
    "schema_names",
    "load_schema",
    "validate",
    "validate_envelope",
    "SCHEMAS_DIR",
]
