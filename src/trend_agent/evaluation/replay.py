"""Walk-forward backtest validity: can history be replayed as it was known at the time?

A backtest is only valid if, at every replay step, it uses exactly the observations the
system could have had then. For each stored series this module reconstructs availability
(``provider_published_at`` when the provider states it, otherwise the conservative
``max(collected_at, ingested_at)``) and classifies the series:

* VALID     observations were collected live (availability within ``live_lag`` of the
            period end) and revisions keep their lineage: replay reproduces what was known.
* LIMITED   part of the history was backfilled; replay must treat backfilled points as
            unavailable before their collection time, so early steps see less data than
            a live system would have - results for that period are labelled limited.
* INVALID   most of the history was backfilled (or timestamps are missing): a replay
            would describe data the system never had in time. Do not report accuracy.

Detection itself is replayed with ``trend-agent detect replay`` (availability-aware);
this module decides whether such a replay may be reported, and scores stored live alerts
against real labels when there are enough of them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from trend_agent.db.repositories.ops import LabelRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.evaluation.detection import score_real

LIVE_LAG = timedelta(days=3)
LIMITED_SHARE = 0.5


def classify(total: int, backfilled: int, missing: int) -> str:
    if total == 0 or missing:
        return "INVALID"
    share = backfilled / total
    if share == 0:
        return "VALID"
    return "LIMITED" if share <= LIMITED_SHARE else "INVALID"


def validity(factory: SessionFactory, *, include_synthetic: bool = False) -> dict[str, Any]:
    with transaction(factory) as s:
        rows = s.execute(
            sa.text(
                "SELECT series_id, bool_or(is_synthetic), count(*), "
                "count(*) FILTER (WHERE COALESCE(provider_published_at, "
                "GREATEST(collected_at, ingested_at)) - period_end > :lag), "
                "count(*) FILTER (WHERE collected_at IS NULL) "
                "FROM ts_observations WHERE (:syn OR NOT is_synthetic) GROUP BY series_id"
            ),
            {"lag": LIVE_LAG, "syn": include_synthetic},
        ).all()
    series = []
    summary = {"VALID": 0, "LIMITED": 0, "INVALID": 0}
    for sid, syn, total, backfilled, missing in rows:
        cls = classify(int(total), int(backfilled), int(missing))
        summary[cls] += 1
        series.append(
            {
                "series_id": sid,
                "synthetic": bool(syn),
                "observations": int(total),
                "backfilled": int(backfilled),
                "class": cls,
            }
        )
    return {
        "live_lag_days": LIVE_LAG.days,
        "series": len(series),
        "by_class": summary,
        "reportable": summary["VALID"] + summary["LIMITED"],
        "detail": series[:200],
        "note": "LIMITED and INVALID series are labelled as such in every backtest result.",
    }


def real_detection(factory: SessionFactory) -> dict[str, Any]:
    """Score stored LIVE alerts (raised in real time, so free of look-ahead) against real
    surge labels; UNKNOWN below the minimum label count."""
    with transaction(factory) as s:
        labels = [
            {"subject_key": r.subject_key, "event_time": r.event_time}
            for r in LabelRepository(s).list(origin="real", kind="surge_event")
        ]
        alerts = [
            {"subject_key": f"target:{tid}", "alerted_at": at}
            for tid, at in s.execute(
                sa.text(
                    "SELECT target_id, first_detected_at FROM surge_events "
                    "WHERE NOT is_synthetic AND first_detected_at IS NOT NULL"
                )
            ).all()
        ]
    return score_real(labels, alerts)
