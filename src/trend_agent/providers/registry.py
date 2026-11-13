"""A deliberately small provider registry (a typed dict, not a plugin framework)."""

from __future__ import annotations

from trend_agent.providers.base import (
    Provider,
    SearchInterestProvider,
    VideoDataProvider,
    require_capability,
)
from trend_agent.providers.errors import CapabilityNotSupportedError, ProviderNotFoundError
from trend_agent.schemas.enums import Capability, ProviderStatus
from trend_agent.schemas.provider import ProviderStatusReport


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        name = provider.descriptor.name
        if name in self._providers:
            raise ValueError(f"provider {name!r} is already registered")
        self._providers[name] = provider

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> list[Provider]:
        return [self._providers[name] for name in self.names()]

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise ProviderNotFoundError(
                f"unknown provider {name!r} (registered: {known})", provider=name
            ) from None

    def search_interest(self, name: str) -> SearchInterestProvider:
        provider = self.get(name)
        require_capability(provider.descriptor, Capability.SEARCH_INTEREST)
        if not isinstance(provider, SearchInterestProvider):
            raise CapabilityNotSupportedError(
                f"provider {name!r} declares search_interest but does not implement it",
                provider=name,
            )
        return provider

    def video_data(self, name: str) -> VideoDataProvider:
        provider = self.get(name)
        require_capability(provider.descriptor, Capability.VIDEO_SEARCH)
        if not isinstance(provider, VideoDataProvider):
            raise CapabilityNotSupportedError(
                f"provider {name!r} declares video_search but does not implement it",
                provider=name,
            )
        return provider

    def with_capability(self, capability: Capability) -> list[Provider]:
        return [p for p in self.all() if p.descriptor.supports(capability)]

    def status_reports(self) -> list[ProviderStatusReport]:
        return [p.status() for p in self.all()]

    def capability_matrix(self) -> dict[str, dict[Capability, ProviderStatus]]:
        """Provider -> capability -> status. Undeclared capabilities are UNSUPPORTED."""
        matrix: dict[str, dict[Capability, ProviderStatus]] = {}
        for report in self.status_reports():
            matrix[report.provider_name] = {
                cap: report.capability_status(cap) for cap in Capability
            }
        return matrix

    async def aclose(self) -> None:
        for provider in self.all():
            await provider.aclose()
