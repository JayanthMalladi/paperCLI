"""Provider adapters and the extension contract.

This phase ships the **interface** (an abstract base + a registry) so that
future adapters (Unpaywall, Crossref, OpenAlex, arXiv, and later DataCite /
PubMed / Semantic Scholar) plug in without touching core. Live network logic
is deliberately not implemented here (Phase 2). See :mod:`papercli.providers.base`.
"""

from papercli.providers.base import ProviderAdapter, CredentialStatus
from papercli.providers.registry import (
    get_registry,
    register_provider,
    registered_providers,
)

__all__ = [
    "ProviderAdapter",
    "CredentialStatus",
    "get_registry",
    "register_provider",
    "registered_providers",
]
