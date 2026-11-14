"""E2E scenarios 1-15: operating modes from real provider state, provider failures and
retries, and budgets - through the real services and database."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from tests.e2e.conftest import NOW, VerifiedYouTubeTrends, make_container
from tests.helpers import MutableClock
from tests.integration.test_monitoring import World
from tests.integration.test_monitoring import world as world
from trend_agent.config import TrendsAccessMode
from trend_agent.container import AppContainer
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.ops import (
    ExperimentRepository,
    LabelRepository,
    OpsAlertRepository,
    ProviderCheckRepository,
    ScheduledRunRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ops.config import OperationsConfig
from trend_agent.ops.modes import Degradation, OperatingMode, SystemCapability
from trend_agent.ops.pipeline import PipelineScheduler, Stage
from trend_agent.ops.signals import SignalClass
from trend_agent.ops.system import SystemService
from trend_agent.providers.errors import (
    AccessRestrictedError,
    QuotaExhaustedError,
    RateLimitedError,
)
from trend_agent.providers.http import RetryPolicy, call_with_retries
from trend_agent.schemas.enums import SearchProperty

pytestmark = pytest.mark.db
C = SystemCapability


def _snap(c: AppContainer) -> Any:
    return SystemService(c).snapshot(with_discovery=False)


def _check(factory: SessionFactory, provider: str, ok: bool, status: str = "available") -> None:
    with transaction(factory) as s:
        ProviderCheckRepository(s).add(
            provider_name=provider,
            now=NOW,
            ok=ok,
            status=status,
            detail="e2e",
            latency_ms=5,
            units_spent=1,
            signal_classes=[],
        )


def _trends(clock: MutableClock) -> AppContainer:
    return make_container(
        clock,
        google_trends_access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        google_trends_credentials_file="/run/secrets/trends.json",
        google_trends_endpoint="https://trends-alpha.example.invalid",
        trends_transport=VerifiedYouTubeTrends(),
    )


# 1
def test_s01_no_real_provider_means_demonstration(container: AppContainer) -> None:
    d = _snap(container).decision
    assert d.base_mode is OperatingMode.DEMONSTRATION
    assert not d.allows(C.COLLECT_REAL_DATA)
    assert not d.direct_search_claims_enabled


# 2
def test_s02_demo_mode_serves_only_the_synthetic_world(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(clock, demo_mode=True)
    try:
        d = _snap(c).decision
        assert d.mode is OperatingMode.DEMONSTRATION
        assert d.demo
    finally:
        c.engine.dispose()


# 3
def test_s03_live_verified_proxy_provider_gives_proxy_research(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(clock, youtube_api_key="AIzaE2EFAKEFAKEFAKEFAKEFAKEFAKE1234")
    try:
        _check(session_factory, "youtube_data_api", ok=True)
        snap = _snap(c)
        yt = next(r for r in snap.readiness if r.name == "youtube_data_api")
        assert yt.live_verified
        assert snap.decision.base_mode is OperatingMode.PROXY_OPPORTUNITY_RESEARCH
        assert snap.decision.allows(C.CONTENT_GAP_ANALYSIS)
        assert not snap.decision.direct_search_claims_enabled
    finally:
        c.engine.dispose()


# 4
def test_s04_external_search_only_gives_limited_intelligence(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    web_only = VerifiedYouTubeTrends(
        supported_properties=frozenset({SearchProperty.GOOGLE_WEB_SEARCH})
    )
    c = make_container(
        clock,
        google_trends_access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        google_trends_credentials_file="/x.json",
        google_trends_endpoint="https://t.example.invalid",
        trends_transport=web_only,
    )
    try:
        d = _snap(c).decision
        assert d.base_mode is OperatingMode.LIMITED_SEARCH_INTELLIGENCE
        assert SignalClass.EXTERNAL_SEARCH_INTEREST in d.classes_usable
        assert not d.direct_search_claims_enabled
    finally:
        c.engine.dispose()


# 5
def test_s05_direct_provider_without_validation_stays_limited(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = _trends(clock)
    try:
        _check(session_factory, "google_trends", ok=True)
        snap = _snap(c)
        assert SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST in snap.decision.classes_verified
        assert snap.decision.base_mode is OperatingMode.LIMITED_SEARCH_INTELLIGENCE
        assert not snap.validation.direct_detection_validated
        assert not snap.decision.direct_search_claims_enabled
    finally:
        c.engine.dispose()


def _validate_direct(factory: SessionFactory, n: int = 30) -> None:
    with transaction(factory) as s:
        for i in range(n):
            LabelRepository(s).upsert(
                origin="real",
                kind="surge_event",
                subject_key=f"target:{i}",
                event_time=NOW - timedelta(days=60 - i),
                known_at=NOW - timedelta(days=50 - i),
                label={},
                source="e2e reviewed surge",
                labeler="analyst",
            )
        ExperimentRepository(s).add(
            name="real detection",
            component="detection",
            hypothesis="h",
            config={},
            dataset={"signal_class": "DIRECT_YOUTUBE_SEARCH_INTEREST"},
            data_origin="real",
            split="test",
            metrics={"precision": 0.82, "recall": 0.71},
            baseline_metrics={},
            decision="adopted",
            decision_reason="met floors",
            code_version="e2e",
        )


# 6
def test_s06_full_mode_requires_verified_direct_provider_and_real_validation(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = _trends(clock)
    try:
        _check(session_factory, "google_trends", ok=True)
        _validate_direct(session_factory)
        snap = _snap(c)
        assert snap.validation.direct_detection_validated
        assert snap.decision.base_mode is OperatingMode.FULL_SEARCH_INTELLIGENCE
        assert snap.decision.direct_search_claims_enabled
    finally:
        c.engine.dispose()


# 7
def test_s07_losing_the_direct_provider_is_recorded_and_withdraws_claims(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    full = _trends(clock)
    try:
        _check(session_factory, "google_trends", ok=True)
        _validate_direct(session_factory)
        svc = SystemService(full)
        svc.record(svc.snapshot(with_discovery=False).decision)
    finally:
        full.engine.dispose()
    plain = make_container(clock)  # the provider is gone
    try:
        from trend_agent.ops.alerts import OpsAlerter

        svc = SystemService(plain)
        al = OpsAlerter(session_factory, clock=clock, cooldown=timedelta(hours=1))
        change = svc.record(svc.snapshot(with_discovery=False).decision, al)
        assert change is not None
        assert change["direct_search_claims_withdrawn"] is True
        assert change["downgrade"] is True
        with transaction(session_factory) as s:
            (alert,) = [a for a in OpsAlertRepository(s).open() if a.kind == "mode_change"]
            assert "WITHDRAWN" in alert.detail
        state = svc.mode_state()
        assert state is not None
        assert len(state["history"]) == 2
    finally:
        plain.engine.dispose()


# 8
def test_s08_failing_live_check_degrades_the_provider_and_the_mode(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(clock, youtube_api_key="AIzaE2EFAKEFAKEFAKEFAKEFAKEFAKE1234")
    try:
        _check(session_factory, "youtube_data_api", ok=False, status="access_restricted")
        snap = _snap(c)
        yt = next(r for r in snap.readiness if r.name == "youtube_data_api")
        assert yt.level.value == "DEGRADED"
        assert not yt.usable_for_real_data
        assert any(d.kind is Degradation.PROVIDER_FAILING for d in snap.decision.degradations)
    finally:
        c.engine.dispose()


# 9
def test_s09_permanent_authorization_errors_are_never_retried() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise AccessRestrictedError("403 forbidden", provider="youtube_data_api", http_status=403)

    with pytest.raises(AccessRestrictedError):
        asyncio.run(
            call_with_retries(
                op, RetryPolicy(max_attempts=5), op_name="t", sleep=lambda d: asyncio.sleep(0)
            )
        )
    assert calls == 1
    calls = 0

    async def quota() -> None:
        nonlocal calls
        calls += 1
        raise QuotaExhaustedError(
            "quota", provider="youtube_data_api", bucket="default", resets_at=None, source="server"
        )

    with pytest.raises(QuotaExhaustedError):
        asyncio.run(
            call_with_retries(
                quota, RetryPolicy(max_attempts=5), op_name="t", sleep=lambda d: asyncio.sleep(0)
            )
        )
    assert calls == 1


# 10
def test_s10_retry_after_is_honoured_and_capped() -> None:
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    attempts = 0

    async def limited() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitedError("429", provider="p", http_status=429, retry_after=2.0)
        return "ok"

    policy = RetryPolicy(max_attempts=4, base_delay=0.01, max_delay=5.0)
    assert asyncio.run(call_with_retries(limited, policy, op_name="t", sleep=sleep)) == "ok"
    assert delays == [2.0, 2.0]  # waits at least Retry-After, with jitter below it

    async def too_long() -> str:
        raise RateLimitedError("429", provider="p", http_status=429, retry_after=3600.0)

    with pytest.raises(RateLimitedError):  # a Retry-After beyond the cap is not slept on
        asyncio.run(call_with_retries(too_long, policy, op_name="t", sleep=sleep))


def _spend(factory: SessionFactory, usd: float, at: Any = NOW) -> None:
    with transaction(factory) as s:
        s.execute(
            sa.text(
                "INSERT INTO recommendation_generation_runs (id, started_at, status, trigger, "
                "profile_id, generator, recommendation_version, configuration_version, "
                "estimated_cost_usd) VALUES (:id, :t, 'completed', 'cli', 'default', 'llm', 'v', "
                "'c', :u)"
            ),
            {"id": uuid.uuid4(), "t": at, "u": usd},
        )


# 11
def test_s11_exhausted_llm_budget_falls_back_to_templates(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(clock, daily_llm_budget=1.0, anthropic_api_key="sk-ant-e2e-fake")
    try:
        _spend(session_factory, 1.5, NOW - timedelta(hours=1))
        svc = c.recommendations()
        svc.cfg = svc.cfg.model_copy(
            update={"llm": svc.cfg.llm.model_copy(update={"enabled": True})}
        )
        gen, note = svc._choose_generator(clock(), 0)
        assert gen.name == svc.template.name
        assert "daily_llm_budget exhausted" in note
    finally:
        c.engine.dispose()


# 12
def test_s12_exhausted_api_budget_skips_scheduled_collection(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(
        clock, daily_api_budget=100, youtube_api_key="AIzaE2EFAKEFAKEFAKEFAKEFAKEFAKE1"
    )
    try:
        _check(session_factory, "youtube_data_api", ok=True)
        with transaction(session_factory) as s:
            s.execute(
                sa.text(
                    "INSERT INTO provider_rate_limits (provider_name, bucket, window_start, "
                    "window_end, effective_limit, units_used) VALUES ('youtube_data_api', "
                    "'default', :a, :b, 9000, 150)"
                ),
                {"a": NOW - timedelta(hours=1), "b": NOW + timedelta(hours=5)},
            )
        ran: list[str] = []
        stages = (
            Stage(
                "monitor_plan",
                lambda ctx: ran.append("plan") or {},  # type: ignore[func-returns-value]
                (C.COLLECT_REAL_DATA,),
            ),
        )
        sched = PipelineScheduler(c, OperationsConfig(), owner="s12", stages=stages)
        assert asyncio.run(sched.tick()).ran == {"monitor_plan": "skipped"}
        assert ran == []
        with transaction(session_factory) as s:
            run = ScheduledRunRepository(s).last("monitor_plan")
            assert run is not None
            assert "api_budget_exhausted" in run.summary["reason"]
    finally:
        c.engine.dispose()


# 13
def test_s13_monthly_llm_budget_degrades_the_mode(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(
        clock, monthly_llm_budget=2.0, youtube_api_key="AIzaE2EFAKEFAKEFAKEFAKEFAKEFAKE1"
    )
    try:
        _check(session_factory, "youtube_data_api", ok=True)
        _spend(session_factory, 1.0, NOW - timedelta(days=2))
        _spend(session_factory, 1.2, NOW - timedelta(days=3))
        d = _snap(c).decision
        assert d.mode is OperatingMode.DEGRADED
        assert not d.allows(C.LLM_GENERATION)
        assert d.allows(C.RECOMMENDATIONS)
    finally:
        c.engine.dispose()


# 14
def test_s14_recommendations_per_run_are_capped(
    session_factory: SessionFactory, clock: MutableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_container(clock, max_recommendations_per_run=3)
    try:
        seen: dict[str, Any] = {}

        def fake_generate(self: Any, **kw: Any) -> Any:
            seen.update(kw)
            raise RuntimeError("stop after recording the call")

        from trend_agent.recommendations.service import RecommendationService

        monkeypatch.setattr(RecommendationService, "generate", fake_generate)
        from trend_agent.ops.pipeline import DEFAULT_STAGES

        stage = next(s for s in DEFAULT_STAGES if s.name == "recommendations")
        sched = PipelineScheduler(
            c, OperationsConfig(), owner="s14", stages=(stage,), synthetic=True
        )
        assert asyncio.run(sched.tick()).ran == {"recommendations": "failed"}
        assert seen["max_opportunities"] == 3
        assert seen["trigger"] == "scheduled"
    finally:
        c.engine.dispose()


# 15
def test_s15_topics_at_the_daily_analysis_limit_are_excluded(world: World) -> None:
    world.svc.sync_targets()
    with transaction(world.factory) as s:
        ids = [t.id for t in MonitoringRepository(s).targets(limit=2)]
    assert len(ids) == 2
    hot, cool = ids
    now = world.clock()
    with transaction(world.factory) as s:
        for tid, n in ((hot, 2), (cool, 1)):
            for _ in range(n):
                s.execute(
                    sa.text(
                        "INSERT INTO content_analysis_runs (id, trigger, canonical_query, status, "
                        "started_at, model_version, analysis_version, configuration_version, "
                        "inputs_fingerprint, demand_basis, target_id) VALUES (:id, 'scheduled', "
                        "'q', 'completed', :t, 'm', 'a', 'c', 'f', 'manual', :tid)"
                    ),
                    {"id": uuid.uuid4(), "t": now - timedelta(hours=2), "tid": tid},
                )
    c = world.container
    c.settings = c.settings.model_copy(update={"max_analysis_requests_per_topic": 2})
    c._budget_guard = None
    assert c.budget_guard().topics_over_analysis_limit(now) == {hot}
    # outside the rolling 24 hours the topic is eligible again
    assert c.budget_guard().topics_over_analysis_limit(now + timedelta(hours=23)) == set()
