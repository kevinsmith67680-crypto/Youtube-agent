"""Point-in-time views, revisions, delays and the causal detector feed."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from tests.unit.forecasting.helpers import DLOADED, at, bundle
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.feature_store import (
    DetectorFeed,
    PointInTimeSeries,
    hours,
    strict_history,
)
from trend_agent.schemas.enums import TimeGranularity


def test_future_observations_are_invisible() -> None:
    spec = syn.Spec(key="pit_a", seed=1, periods=200, lag_h=2)
    pit = PointInTimeSeries(bundle(spec).points)
    t = at(spec, 100)
    vis = pit.at(t)
    assert pit.fast
    # the last visible period ended at least the publication lag before T
    assert float(vis.t_end[-1]) <= hours(t) - 2 + 1e-9
    assert vis.n == 98


def test_revision_visible_only_from_its_own_availability() -> None:
    spec = syn.Spec(
        key="pit_rev",
        seed=2,
        periods=24 * 10,
        revise_every=5,
        revision_delta=7,
        revision_delay_h=30,
    )
    pts = bundle(spec).points
    pit = PointInTimeSeries(pts)
    assert not pit.fast
    period_end = spec.start + timedelta(hours=11)  # index 10 is revised
    initial = next(p for p in pts if p.period_end == period_end and p.revision == 1)
    revised = next(p for p in pts if p.period_end == period_end and p.revision == 2)
    published = revised.provider_published_at
    assert published is not None
    before = pit.at(published - timedelta(minutes=1))
    after = pit.at(published)
    i_before = np.flatnonzero(np.isclose(before.t_end, hours(period_end), rtol=0.0, atol=1e-6))[0]
    i_after = np.flatnonzero(np.isclose(after.t_end, hours(period_end), rtol=0.0, atol=1e-6))[0]
    assert before.values[i_before] == initial.value
    assert after.values[i_after] == revised.value
    assert after.revisions[i_after] == 2


def test_delayed_publication_respected() -> None:
    spec = syn.Spec(
        key="pit_delay",
        seed=3,
        granularity=TimeGranularity.DAY,
        periods=30,
        lag_h=10,
        delayed=((20, 24.0),),
    )
    pit = PointInTimeSeries(bundle(spec).points)
    end20 = hours(spec.start + timedelta(days=21))
    assert not np.any(np.isclose(pit.at(at(spec, 24 * 21 + 20)).t_end, end20, rtol=0.0, atol=1e-6))
    assert np.any(np.isclose(pit.at(at(spec, 24 * 21 + 35)).t_end, end20, rtol=0.0, atol=1e-6))


@pytest.mark.parametrize("cut_h", [24 * 9 + 5, 24 * 12 + 17, 24 * 15 + 2])
def test_fast_detector_feed_equals_strict_replay(cut_h: int) -> None:
    spec = syn.Spec(
        key="pit_det",
        seed=4,
        periods=24 * 16,
        events=(syn.Event("launch", 24 * 11, 2.0, rise_h=24),),
    )
    b = bundle(spec)
    feed = DetectorFeed(b.points, DLOADED, "ai")
    assert feed.pit.fast
    t = at(spec, cut_h)
    fast = feed.history(t)
    strict = strict_history(b.points, DLOADED, t, "ai")
    assert len(fast) == len(strict)
    for a, s in zip(fast[-5:], strict[-5:], strict=True):
        assert a.period_end == s.period_end
        assert a.anomaly.z == s.anomaly.z
        assert a.cusum.statistic == s.cusum.statistic
        assert a.change_point.probability == s.change_point.probability
        assert a.persistence == s.persistence
