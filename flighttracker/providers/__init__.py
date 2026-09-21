"""Provider registry. `--provider <name>` on the CLI resolves through here."""

from __future__ import annotations

from flighttracker.providers.base import Provider, ProviderError
from flighttracker.providers.amadeus import AmadeusProvider
from flighttracker.providers.mock import MockProvider
from flighttracker.providers.serpapi import SerpApiProvider

_REGISTRY: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "amadeus": AmadeusProvider,
    "serpapi": SerpApiProvider,
}
_CACHE: dict[str, Provider] = {}


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str) -> Provider:
    """Return a shared provider instance (they cache auth tokens between polls)."""
    key = name.strip().lower()
    if key not in _REGISTRY:
        raise ProviderError(f"unknown provider {name!r}; available: {', '.join(available())}")
    if key not in _CACHE:
        _CACHE[key] = _REGISTRY[key]()
    return _CACHE[key]


__all__ = ["Provider", "ProviderError", "get_provider", "available",
           "MockProvider", "AmadeusProvider", "SerpApiProvider"]
