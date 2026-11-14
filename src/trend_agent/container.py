"""Composition root: wires settings, database, quota store and providers together.

Everything external (HTTP client, clock, quota store, Trends transport) can be injected,
which is how the test-suite runs the full stack without network access.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import Engine

from trend_agent.config import Settings, get_settings
from trend_agent.db.repositories import SqlQuotaStore
from trend_agent.db.session import SessionFactory, create_session_factory, engine_from_settings
from trend_agent.detection.config import LoadedDetectionConfig, load_detection_config
from trend_agent.detection.service import DetectionService
from trend_agent.discovery.config import ConfigError, LoadedDiscoveryConfig, load_discovery_config
from trend_agent.discovery.providers.factory import register_discovery_providers
from trend_agent.discovery.service import DiscoveryService
from trend_agent.ingestion.retention import RetentionService
from trend_agent.ingestion.service import IngestionService, IngestionSettings
from trend_agent.monitoring.config import LoadedMonitoringConfig, load_monitoring_config
from trend_agent.monitoring.service import MonitoringService
from trend_agent.providers.google_trends import GoogleTrendsProvider, TrendsTransport
from trend_agent.providers.mock import MockSearchInterestProvider
from trend_agent.providers.quota import QuotaStore
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import PROVIDER_NAME as YOUTUBE
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.config import LoadedVerificationConfig, load_verification_config

if TYPE_CHECKING:
    from trend_agent.content_analysis.config import LoadedContentConfig
    from trend_agent.content_analysis.llm_client import AnalysisLLM
    from trend_agent.content_analysis.result_retrieval import ResultSource
    from trend_agent.content_analysis.service import ContentAnalysisService
    from trend_agent.forecasting.config import LoadedForecastingConfig
    from trend_agent.forecasting.service import ForecastingService
    from trend_agent.ops.budgets import BudgetGuard
    from trend_agent.recommendations.backend import DashboardBackend
    from trend_agent.recommendations.config import LoadedRecommendationConfig
    from trend_agent.recommendations.idea_generator import ConceptGenerator
    from trend_agent.recommendations.notifications import Channel
    from trend_agent.recommendations.service import RecommendationService
    from trend_agent.verification.service import VerificationService


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
    _monitoring_config: LoadedMonitoringConfig | None = None
    _detection_config: LoadedDetectionConfig | None = None
    _verification_config: LoadedVerificationConfig | None = None
    _content_config: LoadedContentConfig | None = None
    _forecasting_config: LoadedForecastingConfig | None = None
    _recommendation_config: LoadedRecommendationConfig | None = None
    _budget_guard: BudgetGuard | None = None

    def ingestion(self, *, clock: Clock | None = None) -> IngestionService:
        return IngestionService(
            registry=self.registry,
            session_factory=self.session_factory,
            settings=IngestionSettings(
                max_concurrency=self.settings.ingestion_max_concurrency,
                fetch_timeout_seconds=self.settings.ingestion_fetch_timeout_seconds,
            ),
            clock=clock or self.clock,
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

    def monitoring_config(self) -> LoadedMonitoringConfig:
        if self._monitoring_config is None:
            self._monitoring_config = load_monitoring_config(self.settings.discovery_config_dir)
        return self._monitoring_config

    def monitoring(
        self,
        *,
        clock: Clock | None = None,
        include_synthetic: bool | None = None,
        loaded: LoadedMonitoringConfig | None = None,
    ) -> MonitoringService:
        """Phase 3 monitoring service (collectors are built from registered providers).

        When ``config/detection.yaml`` enables ``integration.run_after_collection``, a
        collection cycle (``run_once``) ends by running Phase 4 detection on the series
        the executor queued.
        """
        hook = None
        try:
            detection_loaded = self.detection_config()
        except ConfigError:
            detection_loaded = None
        if (
            detection_loaded is not None
            and detection_loaded.config.integration.run_after_collection
        ):
            detection = self.detection(clock=clock, include_synthetic=include_synthetic)

            def hook() -> object:
                from trend_agent.schemas.detection import RunType

                return detection.run(
                    run_type=RunType.HOOK, from_queue=True, worker="after-collection"
                )

        return MonitoringService(
            settings=self.settings,
            session_factory=self.session_factory,
            registry=self.registry,
            quota_store=self.quota_store,
            loaded=loaded or self.monitoring_config(),
            clock=clock or self.clock,
            include_synthetic=include_synthetic,
            after_collection=hook,
        )

    def detection_config(self) -> LoadedDetectionConfig:
        if self._detection_config is None:
            self._detection_config = load_detection_config(self.settings.discovery_config_dir)
        return self._detection_config

    def detection(
        self,
        *,
        clock: Clock | None = None,
        include_synthetic: bool | None = None,
        loaded: LoadedDetectionConfig | None = None,
    ) -> DetectionService:
        """Phase 4 detection service."""
        try:
            monitoring = self.monitoring_config()
        except ConfigError:
            monitoring = None
        return DetectionService(
            session_factory=self.session_factory,
            loaded=loaded or self.detection_config(),
            monitoring=monitoring,
            clock=clock or self.clock,
            include_synthetic=include_synthetic,
        )

    def verification_config(self) -> LoadedVerificationConfig:
        if self._verification_config is None:
            self._verification_config = load_verification_config(self.settings.discovery_config_dir)
        return self._verification_config

    def verification(self, *, clock: Clock | None = None) -> VerificationService:
        """Evidence registry + recommendation verification gate."""
        from trend_agent.verification.registry import EvidenceSigner
        from trend_agent.verification.service import VerificationService

        return VerificationService(
            session_factory=self.session_factory,
            loaded=self.verification_config(),
            signer=EvidenceSigner.from_settings(self.settings),
            clock=clock or self.clock,
        )

    def content_config(self) -> LoadedContentConfig:
        from trend_agent.content_analysis.config import load_content_config

        if self._content_config is None:
            self._content_config = load_content_config(self.settings.discovery_config_dir)
        return self._content_config

    def content_analysis(
        self,
        *,
        clock: Clock | None = None,
        source: ResultSource | None = None,
        llm: AnalysisLLM | None = None,
        loaded: LoadedContentConfig | None = None,
    ) -> ContentAnalysisService:
        """Phase 5 search-intent and content-gap analysis.

        Without a YouTube API key the service runs cache-only (stored results); without an
        enabled LLM and key it runs in reduced-capability, deterministic-only mode.
        """
        from trend_agent.content_analysis.service import ContentAnalysisService, make_llm

        loaded = loaded or self.content_config()
        svc = ContentAnalysisService(
            session_factory=self.session_factory,
            loaded=loaded,
            verification=self.verification(clock=clock),
            source=source,
            llm=llm if llm is not None else make_llm(self.settings, loaded),
            clock=clock or self.clock,
            ingestion=self.ingestion(clock=clock),
            settings=self.settings,
        )
        if llm is None:  # an injected LLM (tests) is not a paid call
            svc.llm_budget = self.budget_guard().llm_allowed
        return svc

    def forecasting_config(self) -> LoadedForecastingConfig:
        from trend_agent.forecasting.config import load_forecasting_config

        if self._forecasting_config is None:
            self._forecasting_config = load_forecasting_config(self.settings.discovery_config_dir)
        return self._forecasting_config

    def forecasting(
        self, *, clock: Clock | None = None, loaded: LoadedForecastingConfig | None = None
    ) -> ForecastingService:
        """Phase 6 forecasting (training, registry, inference, drift)."""
        from trend_agent.forecasting.service import ForecastingService

        loaded = loaded or self.forecasting_config()
        return ForecastingService(
            session_factory=self.session_factory,
            loaded=loaded,
            dloaded=self.detection_config(),
            artifact_dir=self.settings.forecast_artifact_dir or loaded.config.artifact_dir,
            clock=clock or self.clock,
        )

    def recommendation_config(self) -> LoadedRecommendationConfig:
        from trend_agent.recommendations.config import load_recommendation_config

        if self._recommendation_config is None:
            self._recommendation_config = load_recommendation_config(
                self.settings.discovery_config_dir
            )
        return self._recommendation_config

    def recommendations(
        self,
        *,
        clock: Clock | None = None,
        generator: ConceptGenerator | None = None,
        channels: Sequence[Channel] | None = None,
        include_synthetic: bool = False,
        loaded: LoadedRecommendationConfig | None = None,
    ) -> RecommendationService:
        """Phase 7 recommendation generation over stored Phase 4-6 results."""
        from trend_agent.recommendations.service import RecommendationService

        clk = clock or self.clock
        svc = RecommendationService(
            session_factory=self.session_factory,
            loaded=loaded or self.recommendation_config(),
            settings=self.settings,
            verification=self.verification(clock=clk),
            content=lambda: self.content_analysis(clock=clk),
            clock=clk,
            generator=generator,
            channels=channels,
            include_synthetic=include_synthetic,
        )
        svc.llm_budget = self.budget_guard().llm_allowed
        return svc

    def budget_guard(self) -> BudgetGuard:
        """Phase 8 spend checks against TREND_AGENT_DAILY_*/MONTHLY_* budgets."""
        from trend_agent.ops.budgets import BudgetGuard

        if self._budget_guard is None:
            self._budget_guard = BudgetGuard(self.settings, self.session_factory, clock=self.clock)
        return self._budget_guard

    def dashboard_backend(
        self, *, clock: Clock | None = None, include_synthetic: bool = False
    ) -> DashboardBackend:
        """The API's data source: the database, or (``demo_mode``) the isolated synthetic
        demonstration, which never reads or writes the database."""
        if self.settings.demo_mode:
            from pathlib import Path

            from trend_agent.discovery.config import resolve_config_dir
            from trend_agent.recommendations.demo import demo_backend

            cache = Path(self.settings.demo_cache_path) if self.settings.demo_cache_path else None
            return demo_backend(resolve_config_dir(self.settings.discovery_config_dir), cache)
        from trend_agent.recommendations.db_backend import DatabaseBackend

        return DatabaseBackend(
            self.recommendations(clock=clock, include_synthetic=include_synthetic),
            self.session_factory,
            self.settings,
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
