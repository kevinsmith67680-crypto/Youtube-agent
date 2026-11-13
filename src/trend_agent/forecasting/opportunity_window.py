"""Opportunity windows: can a creator plausibly publish while demand is still elevated?

For each configured production profile (hypothetical planning assumptions — QUICK_REACTION
2 h, STANDARD_TUTORIAL 8 h, DEEP_DIVE 24 h by default) the earliest publication time is
``prediction time + production hours``. The probability that interest is still elevated
then is the empirical share of comparable historical states (``AnalogModel``) that were
still at or above the breakout threshold at that lead — available only when enough
analogs exist and the lead is observable for the series' granularity (a daily series has
no 2-hour lead: QUICK_REACTION is then UNKNOWN, never interpolated).

Classification (thresholds in ``windows`` config):

* LIKELY_TOO_LATE  — interest is elevated now (or its peak may have passed) and
                     P(elevated at publication) is below ``too_late_probability``;
* URGENT           — elevated at publication is likely, but the estimated peak is within
                     ``urgent_peak_hours`` or the peak may already be passing;
* LONGER_WINDOW    — likely still elevated at ``long_window_hours`` (persistent demand);
* TIMELY           — likely elevated at publication otherwise;
* UNKNOWN          — not estimable, not applicable (no elevated or rising demand), or the
                     forecast itself was withheld.

This estimates demand persistence and timing only. It is not a prediction of views or of
any video's success, and it does not suggest what to produce.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trend_agent.forecasting.config import Windows
from trend_agent.forecasting.peak_estimation import AnalogModel
from trend_agent.schemas.forecast import (
    OpportunityTiming,
    OpportunityWindow,
    PeakEstimate,
    PeakStatus,
)


def windows(
    analogs: AnalogModel | None,
    state: dict[str, float],
    *,
    prediction_time: datetime,
    cfg: Windows,
    peak: PeakEstimate | None,
    relevant: bool,
    max_horizon: int,
    elevated_now: bool = False,
) -> tuple[OpportunityWindow, ...]:
    out = []
    long_p = None
    if analogs is not None and relevant:
        long_p, _, _ = analogs.elevated_at(state, cfg.long_window_hours)
    for name, profile in cfg.profiles.items():
        publish = prediction_time + timedelta(hours=profile.hours)
        evidence: list[str] = []
        if not relevant:
            out.append(
                OpportunityWindow(
                    production_profile=name,
                    production_hours=profile.hours,
                    earliest_publication=publish,
                    probability_elevated_at_publication=None,
                    horizon_covered=profile.hours <= max_horizon,
                    status=OpportunityTiming.UNKNOWN,
                    uncertainty=(
                        "not applicable: interest is neither elevated nor likely to break out"
                    ),
                )
            )
            continue
        p, interval, n = (None, None, 0)
        if analogs is not None:
            p, interval, n = analogs.elevated_at(state, profile.hours)
        if p is None:
            out.append(
                OpportunityWindow(
                    production_profile=name,
                    production_hours=profile.hours,
                    earliest_publication=publish,
                    probability_elevated_at_publication=None,
                    horizon_covered=profile.hours <= max_horizon,
                    status=OpportunityTiming.UNKNOWN,
                    uncertainty=f"not estimable ({n} comparable outcomes at this lead)",
                )
            )
            continue
        evidence.append(f"{n} comparable historical states; {p:.0%} were still elevated")
        peak_soon = (
            peak is not None
            and peak.status is PeakStatus.ESTIMATED
            and peak.median_hours is not None
            and peak.median_hours <= cfg.urgent_peak_hours
        )
        passing = peak is not None and peak.status is PeakStatus.PEAK_MAY_HAVE_PASSED
        if p < cfg.too_late_probability and not (elevated_now or passing):
            # Demand is not (yet) elevated and comparable states rarely were by then:
            # that is "no established demand", not "too late".
            status = OpportunityTiming.UNKNOWN
            evidence.append("interest is not elevated now; no established demand to respond to")
        elif p < cfg.too_late_probability:
            status = OpportunityTiming.LIKELY_TOO_LATE
        elif p >= cfg.elevated_probability and (peak_soon or passing):
            status = OpportunityTiming.URGENT
        elif long_p is not None and long_p >= cfg.elevated_probability:
            status = OpportunityTiming.LONGER_WINDOW
            evidence.append(f"{long_p:.0%} still elevated after {cfg.long_window_hours:.0f} h")
        elif p >= cfg.elevated_probability:
            status = OpportunityTiming.TIMELY
        else:
            status = OpportunityTiming.UNKNOWN
        width = (interval[1] - interval[0]) if interval else None
        out.append(
            OpportunityWindow(
                production_profile=name,
                production_hours=profile.hours,
                earliest_publication=publish,
                probability_elevated_at_publication=p,
                probability_interval=interval,
                horizon_covered=profile.hours <= max_horizon,
                status=status,
                uncertainty=(
                    f"empirical share with a 90 % interval of width {width:.2f}"
                    if width is not None
                    else "interval unavailable"
                ),
                evidence=tuple(evidence),
            )
        )
    return tuple(out)
