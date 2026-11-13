"""Config, policies, monitoring lifecycle, retry classification, tiers, scheduling, freshness."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers import make_settings
from tests.unit.monitoring.helpers import CONFIG, DAY, MONDAY, ROOT
from trend_agent.discovery.config import ConfigError
from trend_agent.monitoring.config import load_monitoring_config
from trend_agent.monitoring.freshness import assess_freshness, estimate_delay
from trend_agent.monitoring.job_state import backoff_delay, classify, decide_retry
from trend_agent.monitoring.lifecycle import (
    EXECUTOR_ACTOR,
    MANAGER_ACTOR,
    MANUAL_ACTOR,
    InvalidMonitoringTransitionError,
    monitoring_transition,
)
from trend_agent.monitoring.policies import PolicyRegistry
from trend_agent.monitoring.scheduler import next_check, period_floor
from trend_agent.monitoring.target_manager import apply_tier_hysteresis, compute_tier
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderNotConfiguredError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.quota import InMemoryQuotaStore
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.monitoring import (
    ErrorCategory,
    FreshnessStatus,
    JobStatus,
    MonitoringStatus,
    MonitoringTier,
)
from trend_agent.schemas.scheduling import DelayEstimate, NextCheck

M = MonitoringStatus
TRENDS = CONFIG.providers["synthetic_trends"]  # daily, 36 h delay, 1 h min poll
NOW = MONDAY + DAY * 10


# ---------------------------------------------------------------------------- config / policies


def test_config_loads_with_documented_capabilities() -> None:
    assert CONFIG.category_budgets.ai == 0.45  # 45/25/30 validated to sum to 1
    trends = CONFIG.providers["google_trends"]
    assert trends.typical_delay == timedelta(days=2)
    assert TimeGranularity.HOUR not in trends.granularities  # no hourly data assumed
    assert trends.supports_youtube_search.value == "unknown"
    assert CONFIG.providers["youtube_data_api"].collection_disabled_reason
    assert CONFIG.providers_for(CONFIG.measurement_preferences[0])[0] == "google_trends"


def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    text = (ROOT / "config" / "monitoring.yaml").read_text()
    bad = tmp_path / "cfg"
    bad.mkdir()
    (bad / "discovery.yaml").write_text("version: 1\n")
    (bad / "monitoring.yaml").write_text(text.replace("min_priority: 55", "min_priority: 30"))
    with pytest.raises(ConfigError, match="hysteresis"):
        load_monitoring_config(bad)
    (bad / "monitoring.yaml").write_text(
        text.replace("retention_policy: synthetic-test-data", "retention_policy: nope", 1)
    )
    with pytest.raises(ConfigError, match="unknown policy"):
        load_monitoring_config(bad)


def test_data_policies() -> None:
    denied = PolicyRegistry(CONFIG, make_settings())
    d = denied.decide("google-trends-alpha", MONDAY)
    assert not d.allowed
    assert "GOOGLE_TRENDS_RETENTION_DAYS" in d.reason
    allowed = PolicyRegistry(CONFIG, make_settings(google_trends_retention_days=90))
    ok = allowed.decide("google-trends-alpha", MONDAY)
    assert ok.allowed
    assert ok.expires_at == MONDAY + timedelta(days=90)
    assert ok.policy_id == "google-trends-alpha@1"
    synthetic = allowed.decide("synthetic-test-data", MONDAY)
    assert synthetic.expires_at is None
    yt = allowed.decide("youtube-api-services", MONDAY)
    assert yt.expires_at == MONDAY + timedelta(days=30)
    derived = allowed.decide(
        "discovery-evidence-derived", MONDAY, source_expiry=MONDAY + timedelta(days=5)
    )
    assert derived.expires_at == MONDAY + timedelta(days=5)  # never outlives its source


# ---------------------------------------------------------------------------- lifecycle


def test_monitoring_transitions() -> None:
    t = monitoring_transition(None, M.PENDING, reason="new", actor=MANAGER_ACTOR, at=NOW)
    assert t.from_status is None
    for a, b in [
        (M.PENDING, M.ACTIVE),
        (M.ACTIVE, M.DORMANT),
        (M.DORMANT, M.ACTIVE),
        (M.ACTIVE, M.PROVIDER_UNAVAILABLE),
        (M.ACTIVE, M.QUOTA_BLOCKED),
        (M.QUOTA_BLOCKED, M.ACTIVE),
        (M.ACTIVE, M.STALE),
        (M.ACTIVE, M.DISABLED),
    ]:
        monitoring_transition(a, b, reason="x", actor=EXECUTOR_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError):
        monitoring_transition(None, M.ACTIVE, reason="x", actor=MANAGER_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError, match="manual"):
        monitoring_transition(M.ACTIVE, M.PAUSED, reason="x", actor=MANAGER_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError, match="manual"):
        monitoring_transition(M.PAUSED, M.ACTIVE, reason="x", actor=MANAGER_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError, match="manual"):
        monitoring_transition(M.DISABLED, M.PENDING, reason="x", actor=MANAGER_ACTOR, at=NOW)
    monitoring_transition(M.DISABLED, M.PENDING, reason="x", actor=MANUAL_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError):
        monitoring_transition(M.DISABLED, M.ACTIVE, reason="x", actor=MANUAL_ACTOR, at=NOW)
    with pytest.raises(InvalidMonitoringTransitionError, match="reason"):
        monitoring_transition(M.PENDING, M.ACTIVE, reason="", actor=MANAGER_ACTOR, at=NOW)


# ---------------------------------------------------------------------------- retries


@pytest.mark.parametrize(
    ("exc", "category", "status"),
    [
        (
            TemporarilyUnavailableError("x", provider="p"),
            ErrorCategory.TRANSIENT,
            JobStatus.RETRY_WAIT,
        ),
        (
            RateLimitedError("x", provider="p", retry_after=120),
            ErrorCategory.RATE_LIMITED,
            JobStatus.RETRY_WAIT,
        ),
        (InvalidCredentialsError("x", provider="p"), ErrorCategory.AUTHORIZATION, JobStatus.FAILED),
        (AccessRestrictedError("x", provider="p"), ErrorCategory.AUTHORIZATION, JobStatus.FAILED),
        (
            CapabilityNotSupportedError("x", provider="p"),
            ErrorCategory.CAPABILITY,
            JobStatus.FAILED,
        ),
        (ProviderNotConfiguredError("x", provider="p"), ErrorCategory.CAPABILITY, JobStatus.FAILED),
        (MalformedResponseError("x", provider="p"), ErrorCategory.VALIDATION, JobStatus.FAILED),
        (RuntimeError("bug"), ErrorCategory.INTERNAL, JobStatus.RETRY_WAIT),
    ],
)
def test_retry_classification(exc: Exception, category: ErrorCategory, status: JobStatus) -> None:
    assert classify(exc) is category
    d = decide_retry(
        exc,
        attempt=1,
        max_attempts=5,
        now=NOW,
        base=timedelta(minutes=1),
        maximum=timedelta(hours=6),
        rng=random.Random(1),
    )
    assert d.status is status
    if status is JobStatus.FAILED:
        assert d.retry_at is None  # permanent errors are never retried
    if isinstance(exc, RateLimitedError):
        assert d.retry_at is not None
        assert d.retry_at >= NOW + timedelta(seconds=120)  # Retry-After respected


def test_quota_defers_until_reset_and_max_attempts_fail() -> None:
    resets = NOW + timedelta(hours=7)
    exc = QuotaExhaustedError(
        "x", bucket="b", resets_at=resets, source="local_estimate", provider="p"
    )
    d = decide_retry(
        exc,
        attempt=5,
        max_attempts=5,
        now=NOW,
        base=timedelta(minutes=1),
        maximum=timedelta(hours=6),
        rng=random.Random(1),
    )
    assert d.status is JobStatus.DEFERRED
    assert d.retry_at == resets
    assert d.deferred
    last = decide_retry(
        TemporarilyUnavailableError("x", provider="p"),
        attempt=5,
        max_attempts=5,
        now=NOW,
        base=timedelta(minutes=1),
        maximum=timedelta(hours=6),
        rng=random.Random(1),
    )
    assert last.status is JobStatus.FAILED


def test_backoff_is_bounded_full_jitter() -> None:
    rng = random.Random(42)
    base, cap = timedelta(minutes=1), timedelta(minutes=10)
    for attempt in range(1, 12):
        delay = backoff_delay(attempt, base, cap, rng)
        assert timedelta(0) <= delay <= min(cap, base * 2 ** (attempt - 1))
    assert backoff_delay(3, base, cap, random.Random(7)) == backoff_delay(
        3, base, cap, random.Random(7)
    )


# ---------------------------------------------------------------------------- tiers


def tier(**kw: object) -> MonitoringTier:
    base: dict[str, object] = {
        "now": NOW,
        "priority": 60.0,
        "novelty_kind": "recurring",
        "entity_type": None,
        "entity_first_seen": None,
        "time_sensitive_until": None,
        "status": M.ACTIVE,
    }
    base.update(kw)
    return compute_tier(CONFIG, **base)[0]  # type: ignore[arg-type]


def test_tiers_are_operational_not_demand() -> None:
    assert (
        tier(
            priority=86,
            novelty_kind="new_version",
            entity_type="ai_model",
            entity_first_seen=NOW - DAY,
        )
        is MonitoringTier.CRITICAL
    )
    event = {
        "priority": 88,
        "novelty_kind": "new_event",
        "entity_type": "game_event",
        "entity_first_seen": NOW - timedelta(days=20),
    }
    assert tier(**event, time_sensitive_until=NOW + DAY) is MonitoringTier.CRITICAL
    assert tier(**event, time_sensitive_until=NOW - DAY) is MonitoringTier.HIGH
    assert tier(priority=58, novelty_kind="evergreen") is MonitoringTier.LOW
    assert tier(priority=70) is MonitoringTier.NORMAL
    assert tier(priority=80) is MonitoringTier.HIGH
    assert tier(priority=40) is MonitoringTier.LOW
    assert tier(status=M.DORMANT) is MonitoringTier.DORMANT
    assert tier(pinned="critical") is MonitoringTier.CRITICAL


def test_tier_hysteresis() -> None:
    hold = timedelta(hours=24)
    up = apply_tier_hysteresis(MonitoringTier.NORMAL, MonitoringTier.CRITICAL, NOW, NOW, hold)
    assert up is MonitoringTier.CRITICAL  # raised immediately
    held = apply_tier_hysteresis(
        MonitoringTier.CRITICAL, MonitoringTier.HIGH, NOW - timedelta(hours=2), NOW, hold
    )
    assert held is MonitoringTier.CRITICAL
    lowered = apply_tier_hysteresis(
        MonitoringTier.CRITICAL, MonitoringTier.HIGH, NOW - timedelta(hours=30), NOW, hold
    )
    assert lowered is MonitoringTier.HIGH


# ---------------------------------------------------------------------------- scheduling


DECLARED = estimate_delay([], TRENDS)


def check(**kw: object) -> NextCheck:
    base: dict[str, object] = {
        "now": NOW,
        "profile": TRENDS,
        "tier_rule": CONFIG.tiers.critical,
        "delay": DECLARED,
        "latest_period_end": None,
        "last_attempt_at": None,
        "last_no_data_at": None,
    }
    base.update(kw)
    return next_check(**base)  # type: ignore[arg-type]


def test_next_check_follows_source_cadence() -> None:
    assert check().eligible  # initial collection
    latest = MONDAY + DAY * 9  # holds data up to day 8 (ends day 9 00:00)
    # Next day ends day 10 00:00 and is published 36 h later.
    early = check(latest_period_end=latest, last_attempt_at=NOW - timedelta(hours=1))
    assert not early.eligible
    assert early.next_at == latest + DAY + timedelta(hours=36)
    due = check(
        now=latest + DAY + timedelta(hours=36),
        latest_period_end=latest,
        last_attempt_at=NOW - timedelta(hours=3),
    )
    assert due.eligible
    # Checked after availability, nothing new: wait the tier's recheck interval.
    after = latest + DAY + timedelta(hours=37)
    recheck = check(
        now=after + timedelta(minutes=30),
        latest_period_end=latest,
        last_attempt_at=after,
        last_no_data_at=after,
    )
    assert not recheck.eligible
    assert recheck.next_at == after + CONFIG.tiers.critical.recheck_after_no_data


def test_minimum_poll_interval_only_delays() -> None:
    latest = MONDAY + DAY * 9
    c = check(
        now=latest + DAY + timedelta(hours=36, minutes=10),
        latest_period_end=latest,
        last_attempt_at=latest + DAY + timedelta(hours=36),
    )
    assert not c.eligible
    assert c.reason == "provider minimum polling interval"
    far = check(latest_period_end=latest, last_attempt_at=NOW)
    assert far.next_at == latest + DAY + timedelta(hours=36)  # not pulled forward to +1 h


def test_polling_every_five_minutes_cannot_create_finer_data() -> None:
    """A daily source checked every 5 minutes for 3 days yields ~1 useful check per day."""
    latest = MONDAY + DAY * 2
    now = MONDAY + DAY * 3
    last_attempt = None
    useful = 0
    end = now + DAY * 3
    while now < end:
        c = check(
            now=now,
            latest_period_end=latest,
            last_attempt_at=last_attempt,
            tier_rule=CONFIG.tiers.critical,
        )
        if c.eligible:
            useful += 1
            last_attempt = now
            # The source publishes day D at end(D) + 36 h; take what is out.
            published_until = period_floor(now - timedelta(hours=36), TimeGranularity.DAY)
            latest = max(latest, published_until)
        now += timedelta(minutes=5)
    assert 2 <= useful <= 4


def test_low_tier_batches_periods_and_force_overrides() -> None:
    latest = MONDAY + DAY * 9
    low = check(latest_period_end=latest, tier_rule=CONFIG.tiers.low)
    assert low.next_at == latest + DAY * 7 + timedelta(hours=36)
    forced = check(latest_period_end=latest, tier_rule=CONFIG.tiers.low, force_at=NOW)
    assert forced.eligible
    irregular = CONFIG.providers["discovery_evidence"]
    c = check(
        profile=irregular,
        latest_period_end=latest,
        last_attempt_at=NOW - timedelta(hours=1),
        delay=estimate_delay([], irregular),
        tier_rule=CONFIG.tiers.normal,
    )
    assert not c.eligible
    assert "irregular" in c.reason


# ---------------------------------------------------------------------------- freshness


def test_delay_estimator_prefers_publication_times() -> None:
    ends = [MONDAY + DAY * i for i in range(1, 6)]
    exact = [(e, e + timedelta(hours=30), e + timedelta(hours=40)) for e in ends]
    est = estimate_delay(exact, TRENDS)
    assert est.basis == "publication_time"
    assert est.p50 == timedelta(hours=30)
    assert est.observed_cadence == DAY
    upper = [(e, None, e + timedelta(hours=50)) for e in ends]
    declared = estimate_delay(upper, TRENDS)
    assert declared.basis == "declared"  # upper bounds never shorten a declared delay
    no_decl = TRENDS.model_copy(update={"typical_delay": None})
    bound = estimate_delay(upper, no_decl)
    assert bound.basis == "collection_upper_bound"
    assert bound.samples_upper_bound == 5


def fresh(**kw: object) -> FreshnessStatus:
    base: dict[str, object] = {
        "now": NOW,
        "profile": TRENDS,
        "provider_available": True,
        "provider_unhealthy": False,
        "latest_period_end": None,
        "last_attempt_at": None,
        "last_no_data_at": None,
        "delay": DelayEstimate(
            p50=timedelta(hours=36),
            p90=timedelta(hours=40),
            samples_exact=5,
            samples_upper_bound=0,
            basis="publication_time",
        ),
    }
    base.update(kw)
    return assess_freshness(**base).status  # type: ignore[arg-type]


def test_freshness_statuses() -> None:
    expected_end = period_floor(NOW - timedelta(hours=36), TimeGranularity.DAY)
    assert fresh(provider_available=False) is FreshnessStatus.SOURCE_UNAVAILABLE
    assert fresh(provider_unhealthy=True) is FreshnessStatus.SOURCE_UNAVAILABLE
    assert fresh() is FreshnessStatus.UNKNOWN_FRESHNESS
    assert fresh(latest_period_end=expected_end) is FreshnessStatus.SOURCE_CURRENT
    behind = expected_end - DAY
    assert fresh(latest_period_end=behind, last_attempt_at=NOW - DAY * 3) is (
        FreshnessStatus.COLLECTION_DELAYED
    )
    assert (
        fresh(
            latest_period_end=behind - DAY * 2,
            last_attempt_at=NOW - timedelta(minutes=5),
            last_no_data_at=NOW - timedelta(minutes=5),
        )
        is FreshnessStatus.PROVIDER_DELAYED
    )
    within = period_floor(NOW - timedelta(hours=36), TimeGranularity.DAY) - DAY
    t = within + DAY + timedelta(hours=37)
    assert fresh(
        now=t + timedelta(minutes=10),
        latest_period_end=within,
        last_attempt_at=t,
        last_no_data_at=t,
    ) in (FreshnessStatus.NO_NEW_OBSERVATION, FreshnessStatus.SOURCE_CURRENT)
    # A low tier that batches 7 periods is current while inside its planned cadence.
    assert (
        fresh(
            latest_period_end=expected_end - DAY * 3,
            last_attempt_at=NOW - DAY * 3,
            planned_periods=7,
        )
        is FreshnessStatus.SOURCE_CURRENT
    )
    irregular = CONFIG.providers["discovery_evidence"]
    assert (
        fresh(
            profile=irregular, latest_period_end=expected_end, delay=estimate_delay([], irregular)
        )
        is FreshnessStatus.UNKNOWN_FRESHNESS
    )


def test_quota_reset_follows_provider_timezone_including_dst() -> None:
    from trend_agent.monitoring.collectors import local_quota

    clock_now = [datetime(2026, 3, 7, 12, tzinfo=UTC)]
    quota = local_quota(TRENDS, InMemoryQuotaStore(), lambda: clock_now[0])
    assert quota is not None
    assert quota.current_window().end == datetime(2026, 3, 8, 8, tzinfo=UTC)  # PST
    clock_now[0] = datetime(2026, 3, 9, 12, tzinfo=UTC)
    assert quota.current_window().end == datetime(2026, 3, 10, 7, tzinfo=UTC)  # PDT
