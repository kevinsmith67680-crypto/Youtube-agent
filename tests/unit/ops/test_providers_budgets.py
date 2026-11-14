"""Signal classification, provider readiness levels, smoke-test bounds and budgets."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tests.helpers import make_settings
from trend_agent.config import TrendsAccessMode
from trend_agent.ops.budgets import BudgetGuard, BudgetState, SpendReport, evaluate
from trend_agent.ops.providers import CheckRecord, ReadinessLevel, assess, smoke_test
from trend_agent.ops.signals import SignalClass, classify
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.providers.mock import MockSearchInterestProvider
from trend_agent.providers.quota import InMemoryQuotaStore
from trend_agent.providers.youtube import YouTubeDataApiProvider

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


def _youtube(key: str | None) -> YouTubeDataApiProvider:
    s = make_settings(youtube_api_key=key)
    return YouTubeDataApiProvider.from_settings(s, InMemoryQuotaStore())


def test_classification_by_what_is_measured() -> None:
    assert classify(_youtube(None).descriptor) == (SignalClass.VIDEO_ACTIVITY,)
    mock = MockSearchInterestProvider(seed=1, enabled=True)
    assert classify(mock.descriptor) == (SignalClass.SYNTHETIC_TEST_DATA,)
    trends = GoogleTrendsProvider(
        access_mode=TrendsAccessMode.DISABLED,
        credentials_file=None,
        endpoint=None,
        retention_days=None,
    )
    # without a verified transport only the web-search property is declared: never YouTube
    assert classify(trends.descriptor) == (SignalClass.EXTERNAL_SEARCH_INTEREST,)


def test_readiness_levels_need_a_recent_successful_live_check() -> None:
    s = make_settings(youtube_api_key="AIzaFAKEFAKEFAKEFAKEFAKEFAKE12345")
    yt = _youtube("AIzaFAKEFAKEFAKEFAKEFAKEFAKE12345")
    no_check = assess([yt], s, checks={}, now=NOW)[0]
    assert no_check.level is ReadinessLevel.CONFIGURED_UNVERIFIED
    assert no_check.usable_for_real_data
    assert not no_check.live_verified
    assert no_check.credentials == "set"
    ok = CheckRecord(NOW - timedelta(hours=1), True, "available", "ok")
    assert (
        assess([yt], s, checks={"youtube_data_api": ok}, now=NOW)[0].level is ReadinessLevel.READY
    )
    old = CheckRecord(NOW - timedelta(days=30), True, "available", "ok")
    assert assess([yt], s, checks={"youtube_data_api": old}, now=NOW)[0].level is (
        ReadinessLevel.CONFIGURED_UNVERIFIED
    )
    failed = CheckRecord(NOW, False, "access_restricted", "403")
    r = assess([yt], s, checks={"youtube_data_api": failed}, now=NOW)[0]
    assert r.level is ReadinessLevel.DEGRADED
    assert not r.usable_for_real_data


def test_unconfigured_and_synthetic_providers() -> None:
    s = make_settings()
    rows = {
        r.name: r
        for r in assess(
            [_youtube(None), MockSearchInterestProvider(seed=1, enabled=True)],
            s,
            checks={},
            now=NOW,
        )
    }
    assert rows["youtube_data_api"].level is ReadinessLevel.NOT_CONFIGURED
    assert rows["youtube_data_api"].credentials == "missing"
    assert rows["youtube_data_api"].approvals_required  # listed, never assumed
    assert rows["mock"].level is ReadinessLevel.SYNTHETIC_ONLY
    assert not rows["mock"].usable_for_real_data


def test_smoke_test_never_calls_an_unconfigured_or_synthetic_provider() -> None:
    res = asyncio.run(smoke_test(_youtube(None)))
    assert not res.attempted
    assert res.units_spent == 0
    res = asyncio.run(smoke_test(MockSearchInterestProvider(seed=1, enabled=True)))
    assert not res.attempted
    assert res.status == "synthetic"


def test_smoke_test_spends_one_unit_on_youtube(make_youtube: Any, fake_youtube: Any) -> None:
    body = {"items": [{"id": "28", "snippet": {"title": "Science", "assignable": True}}]}
    fake_youtube.default("videoCategories", httpx.Response(200, json=body))
    yt = make_youtube()
    res = asyncio.run(smoke_test(yt))
    assert res.attempted
    assert res.ok
    assert res.units_spent == 1
    assert len(fake_youtube.calls("videoCategories")) == 1


def _spend(llm_day: float = 0.0, llm_month: float = 0.0, api: float = 0.0) -> SpendReport:
    return SpendReport(NOW, llm_day, llm_month, {}, api, {})


def test_budget_states() -> None:
    s = make_settings(daily_llm_budget=1.0, monthly_llm_budget=10.0, daily_api_budget=100)
    rep = evaluate(s, _spend(0.5, 2.0, 10))
    assert {ln.name: ln.state for ln in rep.lines} == {
        "daily_api_budget": BudgetState.OK,
        "daily_llm_budget": BudgetState.OK,
        "monthly_llm_budget": BudgetState.OK,
    }
    rep = evaluate(s, _spend(0.85, 2.0, 100))
    assert rep.line("daily_llm_budget").state is BudgetState.WARNING
    assert rep.api_exhausted
    assert not rep.llm_exhausted
    rep = evaluate(s, _spend(0.1, 10.0, 0))
    assert rep.llm_exhausted
    # a zero budget means "no spending", never "unlimited"
    rep = evaluate(make_settings(daily_llm_budget=0.0), _spend())
    assert rep.line("daily_llm_budget").state is BudgetState.EXHAUSTED


def test_budget_guard_refuses_llm_when_exhausted(monkeypatch: Any) -> None:
    import trend_agent.ops.budgets as b

    s = make_settings(daily_llm_budget=1.0)
    monkeypatch.setattr(b, "measure_spend", lambda factory, now: _spend(1.2, 1.2, 0))
    guard = BudgetGuard(s, factory=None, clock=lambda: NOW)  # type: ignore[arg-type]
    ok, why = guard.llm_allowed()
    assert not ok
    assert "daily_llm_budget exhausted" in why
    assert "template" in why
    assert guard.api_allowed() == (True, "")


def test_max_retries_bounds_provider_attempts() -> None:
    assert make_settings(max_retries=0).retry_attempts == 1
    assert make_settings(max_retries=3, http_max_attempts=4).retry_attempts == 4
    assert make_settings(max_retries=9, http_max_attempts=2).retry_attempts == 2
