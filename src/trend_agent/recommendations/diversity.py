"""Final ordering with deduplication and diversity constraints (decisions recorded).

1. Research candidates and rejected concepts are never ranked.
2. Eligible recommendations are sorted by composite score (ties: urgency, then ID).
3. Duplicates are merged (``deduplication``).
4. Greedy selection keeps at most N per opportunity, one per topic x format family (so
   installation and benchmark videos of one model both survive, three installation guides
   do not) and, once enough items exist, at most a configured share per category.
Suppressed recommendations stay stored with the reason; nothing is silently dropped.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.deduplication import find_duplicates, topic_key
from trend_agent.recommendations.format_selector import family
from trend_agent.schemas.recommendation import RecommendationOutput, Suppression

_URGENCY_ORDER = {
    "PUBLISH_SOON": 0,
    "WITHIN_DAYS": 1,
    "FLEXIBLE": 2,
    "UNKNOWN": 3,
    "LIKELY_TOO_LATE": 4,
}


@dataclass
class Selection:
    ranked: list[RecommendationOutput]
    suppressed: list[RecommendationOutput]
    research: list[RecommendationOutput]
    decisions: list[dict[str, object]] = field(default_factory=list)


def _sort_key(r: RecommendationOutput) -> tuple[float, int, str]:
    return (
        -(r.ranking.composite_score or 0.0),
        _URGENCY_ORDER.get(r.production.urgency.value, 3),
        r.recommendation_id,
    )


def select(
    recs: Sequence[RecommendationOutput], loaded: LoadedRecommendationConfig, *, limit: int | None
) -> Selection:
    cfg = loaded.config.diversity
    eligible = sorted([r for r in recs if r.ranking.eligible], key=_sort_key)
    research = [r for r in recs if not r.ranking.eligible]
    out = Selection([], [], research)
    merged = {m.removed: m for m in find_duplicates(eligible, threshold=cfg.title_similarity_merge)}
    per_opp: Counter[str] = Counter()
    per_family: Counter[tuple[str, str]] = Counter()
    per_cat: Counter[str] = Counter()
    target = limit or len(eligible)
    cat_cap = max(1, math.ceil(cfg.max_share_per_category * target))
    pending: list[RecommendationOutput] = []
    for r in eligible:
        reason = None
        dup = None
        if r.recommendation_id in merged:
            m = merged[r.recommendation_id]
            reason, dup = f"duplicate: {m.reason}", m.kept
        elif per_opp[r.opportunity_id] >= cfg.max_per_opportunity:
            reason = f"diversity: already {cfg.max_per_opportunity} ideas for this opportunity"
        elif (
            per_family[(topic_key(r), family(r.video_format))]
            >= cfg.max_per_format_family_per_topic
        ):
            reason = (
                f"diversity: another '{family(r.video_format)}' idea for this topic ranks higher"
            )
        if reason:
            out.suppressed.append(
                r.model_copy(
                    update={
                        "suppression": Suppression(suppressed=True, reason=reason, duplicate_of=dup)
                    }
                )
            )
            out.decisions.append(
                {
                    "id": r.recommendation_id,
                    "decision": "suppressed",
                    "reason": reason,
                    "duplicate_of": dup,
                }
            )
            continue
        cat = r.category or "other"
        if len(eligible) > 3 and per_cat[cat] >= cat_cap:
            pending.append(r)  # admitted only if the list would otherwise be short
            continue
        per_opp[r.opportunity_id] += 1
        per_family[(topic_key(r), family(r.video_format))] += 1
        per_cat[cat] += 1
        out.ranked.append(r)
    for r in pending:
        if len(out.ranked) < target:
            out.ranked.append(r)
            out.decisions.append(
                {
                    "id": r.recommendation_id,
                    "decision": "admitted",
                    "reason": "category cap relaxed: too few other ideas",
                }
            )
        else:
            reason = f"diversity: category share above {cfg.max_share_per_category:.0%}"
            out.suppressed.append(
                r.model_copy(update={"suppression": Suppression(suppressed=True, reason=reason)})
            )
            out.decisions.append(
                {"id": r.recommendation_id, "decision": "suppressed", "reason": reason}
            )
    out.ranked = [
        r.model_copy(update={"ranking": r.ranking.model_copy(update={"rank": i + 1})})
        for i, r in enumerate(sorted(out.ranked, key=_sort_key)[: limit or None])
    ]
    return out
