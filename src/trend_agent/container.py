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

    async def aclose(self) -> None:
        await self.registry.aclose()
        self.engine.dispose()


def build_container(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    clock: Clock = utc_now,
    quota_store: QuotaStore | None = None,
    trends_transport: TrendsTransport | None = None,
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
    )
