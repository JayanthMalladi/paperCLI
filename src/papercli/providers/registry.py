"""Provider registry.

A tiny registry so adapters plug in without core changes. Registration is
explicit: each Phase 2 adapter module calls ``register_provider`` on import.
``get_registry`` returns the module-level instance.
"""

from __future__ import annotations

from typing import Type

from papercli.providers.base import ProviderAdapter

__all__ = [
    "ProviderRegistry",
    "get_registry",
    "register_provider",
    "registered_providers",
]


class ProviderRegistry:
    """Maps provider names to adapter classes and instances."""

    def __init__(self) -> None:
        self._classes: dict[str, Type[ProviderAdapter]] = {}

    def register(self, cls: Type[ProviderAdapter]) -> Type[ProviderAdapter]:
        """Class decorator / method: register an adapter by its info name."""
        self._classes[cls.info.name] = cls
        return cls

    def get(self, name: str) -> ProviderAdapter:
        try:
            cls = self._classes[name]
        except KeyError:
            known = ", ".join(sorted(self._classes)) or "(none)"
            raise KeyError(f"provider {name!r} is not registered; known: {known}") from None
        return cls()

    def names(self) -> list[str]:
        return sorted(self._classes)

    def all(self) -> list[ProviderAdapter]:
        return [self._classes[n]() for n in self.names()]


_registry = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    return _registry


def register_provider(cls: Type[ProviderAdapter]) -> Type[ProviderAdapter]:
    """Module-level helper: ``register_provider(MyAdapter)``."""
    return _registry.register(cls)


def registered_providers() -> list[str]:
    return _registry.names()
