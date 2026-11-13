"""Composition root: wires settings, database, quota store and providers together.

Everything external (HTTP client, clock, quota store, Trends transport) can be injected,
which is how the test-suite runs the full stack without network access.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import Engine

from trend_agent.config import Settings, get_settings
from trend_agent.db.repositories import SqlQuotaStore
from trend_agent.db.session import SessionFactory, create_session_factory, engine_from_settings
from trend_agent.discovery.config import LoadedDiscoveryConfig, load_discovery_config
from trend_agent.discovery.providers.factory import register_discovery_providers
from trend_agent.discovery.service import DiscoveryService
from trend_agent.ingestion.retention import RetentionService
from trend_agent.ingestion.service import IngestionService, IngestionSettings
from trend_agent.providers.google_trends import GoogleTrendsProvider, TrendsTransport
from trend_agent.providers.mock import MockSearchInterestProvider
from trend_agent.providers.quota import QuotaStore
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import PROVIDER_NAME as YOUTUBE
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.utils.time import Clock, utc_now


@dataclass
class AppContainer:
    settings: Settings
    engine: Engine
    session_factory: SessionFactory
    quota_store: QuotaStore
    quota_store_kind: str
    registry: ProviderRegistry
    clock: Clock
    http_client: httpx.AsyncClient | None = None
    _discovery_config: LoadedDiscoveryConfig | None = None

    def ingestion(self) -> IngestionService:
        return IngestionService(
            registry=self.registry,
            session_factory=self.session_factory,
            settings=IngestionSettings(
                max_concurrency=self.settings.ingestion_max_concurrency,
                fetch_timeout_seconds=self.settings.ingestion_fetch_timeout_seconds,
            ),
            clock=self.clock,
        )

    def retention(self) -> RetentionService:
        return RetentionService(self.session_factory, self.clock)

    def youtube(self) -> YouTubeDataApiProvider:
        provider = self.registry.get(YOUTUBE)
        assert isinstance(provider, YouTubeDataApiProvider)
        return provider

    def discovery_config(self) -> LoadedDiscoveryConfig:
        if self._discovery_config is None:
            self._discovery_config = load_discovery_config(self.settings.discovery_config_dir)
        return self._discovery_config

    def discovery(
        self,
        *,
        loaded: LoadedDiscoveryConfig | None = None,
        clock: Clock | None = None,
    ) -> DiscoveryService:
        """Discovery service; registers discovery providers on first use."""
        loaded = loaded or self.discovery_config()
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.http_timeout_seconds),
                follow_redirects=True,
            )
        register_discovery_providers(
            self.registry,
            settings=self.settings,
            loaded=loaded,
            quota_store=self.quota_store,
            clock=self.clock,
            http_client=self.http_client,
        )
        return DiscoveryService(
            registry=self.registry,
            session_factory=self.session_factory,
            loaded=loaded,
            clock=clock or self.clock,
        )

    async def aclose(self) -> None:
        await self.registry.aclose()
        if self.http_client is not None:
            await self.http_client.aclose()
        self.engine.dispose()


def build_container(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    clock: Clock = utc_now,
    quota_store: QuotaStore | None = None,
    trends_transport: TrendsTransport | None = None,
    discovery_http_client: httpx.AsyncClient | None = None,
) -> AppContainer:
    settings = settings or get_settings()
    engine = engine_from_settings(settings)
    factory = create_session_factory(engine)
    store: QuotaStore = quota_store if quota_store is not None else SqlQuotaStore(factory)
    registry = ProviderRegistry()
    registry.register(
        YouTubeDataApiProvider.from_settings(settings, store, http_client=http_client, clock=clock)
    )
    registry.register(
        GoogleTrendsProvider.from_settings(settings, transport=trends_transport, clock=clock)
    )
    registry.register(
        MockSearchInterestProvider(
            seed=settings.mock_seed, enabled=settings.mock_provider_enabled, clock=clock
        )
    )
    return AppContainer(
        settings=settings,
        engine=engine,
        session_factory=factory,
        quota_store=store,
        quota_store_kind="database" if quota_store is None else type(store).__name__,
        registry=registry,
        clock=clock,
        http_client=discovery_http_client,
    )
