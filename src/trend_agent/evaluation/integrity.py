"""End-to-end pipeline integrity over stored data.

Counts every stage (observations -> surge events -> analyses -> opportunities ->
forecasts -> recommendations), separately for REAL and SYNTHETIC data, and checks the
invariants that make downstream results trustworthy:

* no real downstream record derives from synthetic upstream evidence (and vice versa);
* no recommendation without its opportunity; no opportunity without its analysis run;
* no forecast for a series that has no observations;
* proxy observations are never stored under a search-interest category.

A violated invariant is an integrity failure, not a statistic.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from trend_agent.db.session import SessionFactory, transaction

STAGES = (
    ("observations", "ts_observations"),
    ("surge_events", "surge_events"),
    ("analysis_runs", "content_analysis_runs"),
    ("opportunities", "content_opportunities"),
    ("forecasts", "forecast_predictions"),
    ("recommendations", "video_recommendations"),
)

CHECKS = {
    "real_opportunity_from_synthetic_analysis": (
        "SELECT count(*) FROM content_opportunities o JOIN content_analysis_runs r "
        "ON r.id = o.analysis_run_id WHERE o.is_synthetic <> r.is_synthetic"
    ),
    "analysis_synthetic_mismatch_with_surge": (
        "SELECT count(*) FROM content_analysis_runs r JOIN surge_events e "
        "ON e.id = r.surge_event_id WHERE r.is_synthetic <> e.is_synthetic"
    ),
    "recommendation_synthetic_mismatch_with_opportunity": (
        "SELECT count(*) FROM video_recommendations v JOIN content_opportunities o "
        "ON o.id = v.opportunity_row_id WHERE v.is_synthetic <> o.is_synthetic"
    ),
    "recommendation_without_opportunity": (
        "SELECT count(*) FROM video_recommendations v LEFT JOIN content_opportunities o "
        "ON o.id = v.opportunity_row_id WHERE v.opportunity_row_id IS NOT NULL AND o.id IS NULL"
    ),
    "opportunity_without_analysis_run": (
        "SELECT count(*) FROM content_opportunities o LEFT JOIN content_analysis_runs r "
        "ON r.id = o.analysis_run_id WHERE o.analysis_run_id IS NOT NULL AND r.id IS NULL"
    ),
    "forecast_without_observations": (
        "SELECT count(*) FROM forecast_predictions f WHERE NOT EXISTS "
        "(SELECT 1 FROM ts_observations o WHERE o.series_id = f.series_id)"
    ),
    "surge_synthetic_mismatch_with_observations": (
        "SELECT count(*) FROM surge_events e WHERE EXISTS (SELECT 1 FROM ts_observations o "
        "WHERE o.series_id = e.series_id AND o.is_synthetic <> e.is_synthetic)"
    ),
    "proxy_stored_as_search_interest": (
        "SELECT count(*) FROM ts_observations WHERE is_proxy AND measurement_category IN "
        "('direct_youtube_search_interest','external_search_interest')"
    ),
}


def check(factory: SessionFactory) -> dict[str, Any]:
    with transaction(factory) as s:
        stages = {}
        for name, table in STAGES:
            rows = s.execute(
                sa.text(f"SELECT is_synthetic, count(*) FROM {table} GROUP BY 1")  # noqa: S608  # nosec B608
            ).all()
            counts = {"real": 0, "synthetic": 0}
            for syn, n in rows:
                counts["synthetic" if syn else "real"] = int(n)
            stages[name] = counts
        violations = {k: int(s.execute(sa.text(q)).scalar_one()) for k, q in CHECKS.items()}
    failed = {k: v for k, v in violations.items() if v}
    return {
        "stages": stages,
        "invariants": violations,
        "status": "PASS" if not failed else "FAIL",
        "failed": failed,
        "real_pipeline_exercised": all(stages[n]["real"] > 0 for n, _ in STAGES),
    }
