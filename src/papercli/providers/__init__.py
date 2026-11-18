"""Provider adapters and the extension contract.

The abstract base + registry are the core contract; the live network
adapters (Unpaywall, Crossref, OpenAlex, arXiv, Semantic Scholar) and
the shared HTTP/retry/cache layer plug in here (Phase 2). Importing this
module registers every shipped adapter on the registry.
"""

from papercli.providers.base import (
    CredentialStatus,
    ProviderAdapter,
    ProviderInfo,
)
from papercli.providers.registry import (
    get_registry,
    register_provider,
    registered_providers,
)
# Importing the adapter modules triggers their register_provider calls.
from papercli.providers import arxiv as _arxiv_mod
from papercli.providers import crossref as _crossref_mod
from papercli.providers import openalex as _openalex_mod
from papercli.providers import semantic_scholar as _s2_mod
from papercli.providers import unpaywall as _unpaywall_mod
from papercli.providers.arxiv import ArxivAdapter
from papercli.providers.crossref import CrossrefAdapter
from papercli.providers.openalex import OpenAlexAdapter
from papercli.providers.semantic_scholar import SemanticScholarAdapter
from papercli.providers.unpaywall import UnpaywallAdapter

__all__ = [
    "ArxivAdapter",
    "CrossrefAdapter",
    "OpenAlexAdapter",
    "SemanticScholarAdapter",
    "UnpaywallAdapter",
    "CredentialStatus",
    "ProviderAdapter",
    "ProviderInfo",
    "get_registry",
    "register_provider",
    "registered_providers",
]
