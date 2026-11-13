"""MonitoringService: composition of target manager, planner, executor and read models."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trend_agent.config import Settings
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.collectors import (
    REQUEST_BUCKET,
    Collector,
    CollectorRegistry,
    EventSignalCollector,
    SearchInterestCollector,
    as_search_interest,
    local_quota,
)
from trend_agent.monitoring.config import LoadedMonitoringConfig, MonitoringConfig
from trend_agent.monitoring.executor import Executor, WorkerReport
from trend_agent.monitoring.freshness import estimate_delay
from trend_agent.monitoring.planner import Planner
from trend_agent.monitoring.policies import PolicyRegistry
from trend_agent.monitoring.target_manager import SyncReport, TargetManager
from trend_agent.providers.quota import QuotaStore
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.schemas.scheduling import CollectorKind, DelayEstimate, PlanReport
from trend_agent.utils.time import Clock

_log = get_logger(__name__)


def build_collectors(
    *,
    config: MonitoringConfig,
    registry: ProviderRegistry,
    policies: PolicyRegistry,
    quota_store: QuotaStore,
    session_factory: SessionFactory,
    clock: Clock,
    extra: Iterable[Collector] = (),
) -> CollectorRegistry:
    collectors = CollectorRegistry(config.providers)
    registered = set(registry.names())
    for name, profile in config.providers.items():
        if not profile.enabled or profile.collection_disabled_reason:
            continue
        if profile.collector is CollectorKind.SEARCH_INTEREST and name in registered:
            provider = as_search_interest(registry.get(name))
            if provider is None:
                continue
            quota = (
                None
                if profile.quota.use_provider_quota
                else local_quota(profile, quota_store, clock)
            )
            collectors.add(
                SearchInterestCollector(
                    provider, profile, policies=policies, quota=quota, clock=clock
                )
            )
        elif profile.collector is CollectorKind.EVENT_SIGNALS:
            collectors.add(
                EventSignalCollector(
                    session_factory,
                    profile,
                    policies=policies,
                    compliance=config.compliance,
                    clock=clock,
                )
            )
    for collector in extra:
        collectors.add(collector)
    return collectors


@dataclass
class RunOnceReport:
    sync: SyncReport
    plan: PlanReport
    workers: list[WorkerReport]
    detection: Any = None  # Phase 4 hook result (DetectionReport) when enabled


@dataclass(frozen=True)
class ProviderOverview:
    name: str
    display_name: str
    collector: str
    synthetic: bool
    measurement_categories: list[str]
    available: bool
    status: str
    detail: str
    health: str
    consecutive_failures: int
    delay: DelayEstimate | None
    quota: dict[str, Any] | None
    profile: dict[str, Any]


class MonitoringService:
    def __init__(
        self,
        *,
        settings: Settings | None,
        session_factory: SessionFactory,
        registry: ProviderRegistry,
        quota_store: QuotaStore,
        loaded: LoadedMonitoringConfig,
        clock: Clock,
        include_synthetic: bool | None = None,
        extra_collectors: Iterable[Collector] = (),
        after_collection: Callable[[], Any] | None = None,
    ) -> None:
        # Phase 4 integration: called after a collection cycle stored observations (the
        # detection service processes the series the executor queued in its outbox).
        self.after_collection = after_collection
        self.loaded = loaded
        self.config = loaded.config
        self._factory = session_factory
        self._clock = clock
        self.policies = PolicyRegistry(self.config, settings)
        self.collectors = build_collectors(
            config=self.config,
            registry=registry,
            policies=self.policies,
            quota_store=quota_store,
            session_factory=session_factory,
            clock=clock,
            extra=extra_collectors,
        )
        self.targets = TargetManager(
            session_factory,
            self.config,
            self.collectors,
            clock=clock,
            include_synthetic=include_synthetic,
        )
        self.planner = Planner(session_factory, self.config, self.collectors, clock=clock)
        self.ensure_policies()

    @property
    def session_factory(self) -> SessionFactory:
        return self._factory

    def now(self) -> datetime:
        return self._clock()

    def ensure_policies(self) -> None:
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            for policy in self.policies.all():
                repo.upsert_policy(policy.row())

    def executor(self, worker_id: str | None = None, rng: random.Random | None = None) -> Executor:
        return Executor(
            self._factory,
            self.config,
            self.collectors,
            clock=self._clock,
            worker_id=worker_id,
            rng=rng,
        )

    def sync_targets(self) -> SyncReport:
        return self.targets.sync()

    def plan(self, *, dry_run: bool = False) -> PlanReport:
        return self.planner.plan(dry_run=dry_run)

    async def run_once(self, *, worker_id: str = "run-once") -> RunOnceReport:
        sync = self.sync_targets()
        plan = self.plan()
        workers = await self.executor(worker_id).drain()
        detection = None
        if self.after_collection is not None:
            try:
                detection = self.after_collection()
            except Exception as exc:  # collection succeeded; detection retries from its queue
                log_event(
                    _log,
                    "monitor.detection_hook_failed",
                    logging.ERROR,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
        return RunOnceReport(sync, plan, workers, detection)

    # ------------------------------------------------------------------ read models
    def providers(self) -> list[ProviderOverview]:
        out = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            for name, profile in sorted(self.config.providers.items()):
                avail = self.collectors.availability(name)
                health = repo.health(name)
                delay = estimate_delay(ts.delay_samples(name), profile)
                quota_info = None
                if name in self.collectors.names():
                    quota = self.collectors.get(name).quota_manager()
                    if quota is not None:
                        usage = quota.usage(REQUEST_BUCKET)
                        quota_info = {
                            "bucket": usage.bucket,
                            "used": usage.used,
                            "limit": usage.effective_limit,
                            "daily_limit": usage.daily_limit,
                            "remaining": usage.remaining,
                            "window_end": usage.window_end.isoformat(),
                            "reset_timezone": quota.policy.reset_timezone,
                        }
                out.append(
                    ProviderOverview(
                        name=name,
                        display_name=profile.display_name or name,
                        collector=profile.collector.value,
                        synthetic=profile.synthetic,
                        measurement_categories=[c.value for c in profile.measurement_categories],
                        available=avail.available,
                        status=avail.status,
                        detail=avail.detail,
                        health=health.status.value if health else "unknown",
                        consecutive_failures=health.consecutive_failures if health else 0,
                        delay=delay,
                        quota=quota_info,
                        profile=profile.describe(),
                    )
                )
        return out
