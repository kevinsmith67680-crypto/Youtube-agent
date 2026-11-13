"""Observable competing coverage within the inspected result set.

Every number is scoped to what was retrieved and inspected ("Of the 30 retrieved results
inspected, six titles and descriptions directly matched the tutorial intent"), never a
claim about all of YouTube. The API's ``totalResults`` is kept as an *estimate* only and
never used as a count. View counts are not used at all: they measure popularity, not
search demand or satisfaction, and storing statistics / building derived metrics needs
YouTube API Services authorisation this project does not assume.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta

from trend_agent.schemas.content_gap import SupplyClass, SupplySummary
from trend_agent.schemas.search_result import (
    CandidateVideo,
    EntityMatch,
    FreshnessAssessment,
    FreshnessStatus,
    RelevanceAssessment,
    RetrievalStatus,
    RetrievedResultSet,
)

DISABLED_METRICS = (
    "view-count based popularity (popularity is not demand; derived metrics need authorisation)",
    "total competing videos on YouTube (the API reports only an estimate for one request)",
)


def summarise(
    results: RetrievedResultSet,
    videos: Sequence[CandidateVideo],
    relevance: Sequence[RelevanceAssessment],
    freshness: Sequence[FreshnessAssessment],
    *,
    now: datetime,
    recent_window: timedelta,
    intent_label: str,
) -> SupplySummary:
    if results.status is RetrievalStatus.UNAVAILABLE or not videos:
        return SupplySummary(
            supply_class=SupplyClass.UNKNOWN_SUPPLY,
            scope="No results could be retrieved; supply is unknown.",
            inspected=0,
            relevant=0,
            highly_relevant=0,
            recent_relevant=0,
            exact_entity=0,
            older_version=0,
            distinct_channels=0,
            api_total_estimate=results.approx_total_reported,
            disabled_metrics=DISABLED_METRICS,
        )
    by_id = {v.video_id: v for v in videos}
    fresh = {f.video_id: f for f in freshness}
    rel = [r for r in relevance if r.relevant]
    high = [r for r in rel if r.highly_relevant]
    recent = [
        r
        for r in rel
        if (p := by_id[r.video_id].published_at) is not None and now - p <= recent_window
    ]
    exact = [r for r in relevance if r.entity_match is EntityMatch.EXACT]
    older = [r for r in relevance if r.entity_match is EntityMatch.VERSION_MISMATCH]
    family = [
        r
        for r in relevance
        if r.entity_match in (EntityMatch.EXACT, EntityMatch.FAMILY, EntityMatch.VERSION_MISMATCH)
    ]
    formats = Counter(r.video_format.value for r in relevance)
    direct_formats = Counter(r.video_format.value for r in rel)
    channels = {by_id[r.video_id].channel_id for r in rel}
    n = len(videos)
    statements = [
        f"Of the {n} retrieved results inspected, {len(rel)} appear relevant to the "
        f"{intent_label} intent from their titles and descriptions, and {len(high)} "
        f"{'matches' if len(high) == 1 else 'match'} it closely.",
    ]
    if family:
        statements.append(
            f"{len(older)} of {len(family)} results about the entity's family concern another "
            "version."
        )
    outdated = sum(
        1
        for r in rel
        if fresh.get(r.video_id)
        and fresh[r.video_id].status
        in (FreshnessStatus.POTENTIALLY_OUTDATED, FreshnessStatus.OUTDATED_WITH_EVIDENCE)
    )
    if rel:
        statements.append(
            f"{len(recent)} of the {len(rel)} relevant results were published within "
            f"{recent_window.days} days; {outdated} may be outdated."
        )
    return SupplySummary(
        supply_class=SupplyClass.OBSERVED_SUPPLY,
        scope=statements[0],
        inspected=n,
        relevant=len(rel),
        highly_relevant=len(high),
        recent_relevant=len(recent),
        exact_entity=len(exact),
        older_version=len(older),
        distinct_channels=len(channels),
        formats=dict(formats),
        directly_relevant_formats=dict(direct_formats),
        share_older_version=round(len(older) / len(family), 3) if family else None,
        api_total_estimate=results.approx_total_reported,
        statements=tuple(statements),
        disabled_metrics=DISABLED_METRICS,
    )
