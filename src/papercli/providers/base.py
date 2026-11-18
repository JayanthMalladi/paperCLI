"""Provider extension contract.

A small abstract base + registry so future adapters (Unpaywall, Crossref,
OpenAlex, arXiv, and later DataCite / PubMed / Semantic Scholar) plug in
without touching core. Live network logic is Phase 2; this phase ships the
interface and a credential-presence probe so ``papers doctor`` can report
each provider's readiness from config + environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

__all__ = ["CredentialStatus", "ProviderAdapter", "ProviderInfo"]


class CredentialStatus(Enum):
    """Readiness of a provider's credentials (values are safe to emit)."""

    NOT_REQUIRED = "not_required"   # no credential needed (e.g. arXiv)
    PRESENT = "present"           # the referenced env var is set
    MISSING = "missing"          # the referenced env var is unset
    DISABLED = "disabled"        # provider intentionally disabled in config


@dataclass(frozen=True)
class ProviderInfo:
    """Static descriptor for a registered provider."""

    name: str
    #: name of the config section under [providers.<name>]
    credential_env: str = ""
    #: True when the provider needs no credential at all.
    credential_optional: bool = False
    base_url: str = ""


class ProviderAdapter(ABC):
    """Interface every concrete provider adapter implements.

    Core code (storage, research, jobs) depends only on this contract. The
    registry hands back instances; nothing in core knows a specific
    provider's module.
    """

    #: Override in subclasses.
    info: ProviderInfo

    @abstractmethod
    def credential_status(self, config: Any, env: Mapping[str, str] | None = None) -> CredentialStatus:
        """Report credential readiness without making a network call.

        ``config`` is a ``papercli.config.Config``; ``env`` may carry the
        local ``.env`` values on top of the process environment.
        """

    @abstractmethod
    def preflight(self, config: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Cheap, mostly-offline readiness check used by ``papers doctor``.

        Returns a JSON-serialisable dict. Must not require the network and
        must not leak secret values -- use masked/presence-only reporting.
        """

    def name(self) -> str:
        return self.info.name
