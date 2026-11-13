"""Source freshness: how late does a provider publish, and is a target up to date?

The delay estimator learns from *observed* behaviour:

* exact samples: ``provider_published_at - period_end`` when the provider states when it
  published a value;
* upper bounds: ``first collected_at - period_end`` when it does not (the true delay is at
  most this - we may simply have asked late). Upper bounds never shorten a declared delay.

Estimates are medians/90th percentiles of recent samples and are reported as estimates;
the declared profile delay is used until enough samples exist.

Freshness of a target (one provider, one series) is decided in this order:

1. provider unavailable / unhealthy                         -> SOURCE_UNAVAILABLE
2. nothing collected yet, or cadence unknown                -> UNKNOWN_FRESHNESS
                                                               (NO_NEW_OBSERVATION if the
                                                               last check found nothing)
3. we hold the latest period the source should have out     -> SOURCE_CURRENT
   (or are behind only by the target's planned batching, e.g. a low tier that fetches
   seven daily periods per request; the reason says so)
4. behind, and not checked since that period became due     -> COLLECTION_DELAYED
5. behind, checked after it became due, beyond p90 + grace  -> PROVIDER_DELAYED
6. behind, checked, but still within the normal delay range -> NO_NEW_OBSERVATION
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.monitoring.scheduler import period_floor, period_step
from trend_agent.schemas.monitoring import FreshnessStatus
from trend_agent.schemas.scheduling import DelayEstimate, ProviderProfile

MIN_EXACT_SAMPLES = 3
GRACE = timedelta(hours=1)


def _quantile(values: Sequence[timedelta], q: float) -> timedelta:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = q * (len(ordered) - 1)
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    frac = index - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def estimate_delay(
    samples: Sequence[tuple[datetime, datetime | None, datetime]],
    profile: ProviderProfile,
) -> DelayEstimate:
    """``samples``: (period_end, provider_published_at, first collected_at)."""
    exact = [pub - end for end, pub, _ in samples if pub is not None and pub >= end]
    upper = [col - end for end, pub, col in samples if pub is None and col >= end]
    ends = sorted({end for end, _, _ in samples})
    cadence = None
    if len(ends) >= 3:
        gaps = [b - a for a, b in itertools.pairwise(ends) if b > a]
        cadence = statistics.median_low(gaps) if gaps else None
    declared = profile.typical_delay
    if len(exact) >= MIN_EXACT_SAMPLES:
        return DelayEstimate(
            p50=_quantile(exact, 0.5),
            p90=_quantile(exact, 0.9),
            samples_exact=len(exact),
            samples_upper_bound=len(upper),
            basis="publication_time",
            observed_cadence=cadence,
            declared_delay=declared,
        )
    if declared is not None:
        return DelayEstimate(
            p50=declared,
            p90=declared,
            samples_exact=len(exact),
            samples_upper_bound=len(upper),
            basis="declared",
            observed_cadence=cadence,
            declared_delay=declared,
        )
    if upper:
        # Only an upper bound is known: use the smallest observed bound (conservative for
        # scheduling would be the largest; the smallest avoids waiting needlessly long).
        return DelayEstimate(
            p50=min(upper),
            p90=_quantile(upper, 0.9),
            samples_exact=0,
            samples_upper_bound=len(upper),
            basis="collection_upper_bound",
            observed_cadence=cadence,
            declared_delay=None,
        )
    return DelayEstimate(
        p50=None,
        p90=None,
        samples_exact=0,
        samples_upper_bound=0,
        basis="none",
        observed_cadence=cadence,
        declared_delay=None,
    )


def expected_latest_period_end(
    now: datetime, profile: ProviderProfile, delay: timedelta
) -> datetime:
    """End of the latest period the source should have published by ``now``."""
    step = period_step(profile.collection_granularity)
    return period_floor(now - delay, profile.collection_granularity) if step else now


@dataclass(frozen=True)
class FreshnessAssessment:
    status: FreshnessStatus
    reason: str
    latest_period_end: datetime | None
    expected_period_end: datetime | None
    expected_available_at: datetime | None
    delay: DelayEstimate | None


def assess_freshness(
    *,
    now: datetime,
    profile: ProviderProfile | None,
    provider_available: bool,
    provider_unhealthy: bool,
    latest_period_end: datetime | None,
    last_attempt_at: datetime | None,
    last_no_data_at: datetime | None,
    delay: DelayEstimate | None,
    planned_periods: int = 1,
) -> FreshnessAssessment:
    def result(
        status: FreshnessStatus,
        reason: str,
        expected: datetime | None = None,
        available: datetime | None = None,
    ) -> FreshnessAssessment:
        return FreshnessAssessment(status, reason, latest_period_end, expected, available, delay)

    if profile is None or not provider_available:
        return result(FreshnessStatus.SOURCE_UNAVAILABLE, "provider not available")
    if provider_unhealthy:
        return result(FreshnessStatus.SOURCE_UNAVAILABLE, "provider unhealthy (repeated failures)")
    no_data_last = (
        last_no_data_at is not None
        and last_attempt_at is not None
        and last_no_data_at >= last_attempt_at
    )
    if latest_period_end is None:
        if no_data_last:
            return result(FreshnessStatus.NO_NEW_OBSERVATION, "checked; no observations yet")
        return result(FreshnessStatus.UNKNOWN_FRESHNESS, "no observations collected yet")
    if profile.publication_cadence is None or delay is None or delay.expected is None:
        if no_data_last:
            return result(
                FreshnessStatus.NO_NEW_OBSERVATION, "irregular source; last check found nothing new"
            )
        return result(FreshnessStatus.UNKNOWN_FRESHNESS, "source cadence unknown")
    expected_delay = delay.expected
    expected_end = expected_latest_period_end(now, profile, expected_delay)
    if latest_period_end >= expected_end:
        return result(
            FreshnessStatus.SOURCE_CURRENT,
            "latest expected period is stored",
            expected_end,
            expected_end + expected_delay,
        )
    step = period_step(profile.collection_granularity) or timedelta(days=1)
    first_missing_end = latest_period_end + step
    due_at = first_missing_end + expected_delay
    if planned_periods > 1 and now < latest_period_end + step * planned_periods + expected_delay:
        planned_at = latest_period_end + step * planned_periods + expected_delay
        return result(
            FreshnessStatus.SOURCE_CURRENT,
            f"within the planned cadence: this tier collects {planned_periods} periods per "
            f"request; next collection due {planned_at.isoformat()}",
            expected_end,
            planned_at,
        )
    if last_attempt_at is None or last_attempt_at < due_at:
        return result(
            FreshnessStatus.COLLECTION_DELAYED,
            f"period ending {first_missing_end.isoformat()} expected by {due_at.isoformat()}; "
            "not checked since",
            first_missing_end,
            due_at,
        )
    p90 = delay.p90 or expected_delay
    if now > first_missing_end + p90 + GRACE:
        return result(
            FreshnessStatus.PROVIDER_DELAYED,
            f"checked after expected availability; period ending {first_missing_end.isoformat()} "
            f"still unpublished beyond the {p90} p90 delay",
            first_missing_end,
            due_at,
        )
    return result(
        FreshnessStatus.NO_NEW_OBSERVATION,
        "checked; next period not yet published (within normal delay range)",
        first_missing_end,
        due_at,
    )
