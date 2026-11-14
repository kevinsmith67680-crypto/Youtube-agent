"""Deterministic duplicate detection for recommendation concepts.

Titles are normalised (lower case, stop words removed, synonyms folded: "installation",
"installing", "setup" -> "install"; "tutorial", "walkthrough" -> "guide"; ...). Two concepts
are duplicates when they serve the same informational objective (same topic and format
family) and their normalised titles are identical or overlap strongly (Jaccard >= the
configured threshold). "Install Model X" and "Benchmark Model X" have different families and
are kept; "How to Install Model X" and "Model X Installation Guide" are merged. Every merge
is recorded. No embeddings are needed for this deterministic first pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trend_agent.recommendations.format_selector import family
from trend_agent.recommendations.title_generator import tokens
from trend_agent.schemas.recommendation import RecommendationOutput, VideoFormat


def normalized(title: str) -> frozenset[str]:
    return frozenset(tokens(title))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def topic_key(r: RecommendationOutput) -> str:
    return str(r.topic_cluster_id) if r.topic_cluster_id is not None else r.primary_query.lower()


@dataclass(frozen=True)
class Merge:
    kept: str
    removed: str
    reason: str
    similarity: float


def same_objective(a: RecommendationOutput, b: RecommendationOutput) -> bool:
    return topic_key(a) == topic_key(b) and family(a.video_format) == family(b.video_format)


def find_duplicates(recs: Sequence[RecommendationOutput], *, threshold: float) -> list[Merge]:
    """Pairs to merge, best-ranked first wins. Input order = preference order."""
    merges: list[Merge] = []
    kept: list[RecommendationOutput] = []
    for r in recs:
        dup = None
        for k in kept:
            ta, tb = normalized(r.title), normalized(k.title)
            sim = jaccard(ta, tb)
            if ta == tb:
                dup = Merge(k.recommendation_id, r.recommendation_id, "exact duplicate title", 1.0)
            elif same_objective(r, k) and sim >= threshold:
                dup = Merge(
                    k.recommendation_id,
                    r.recommendation_id,
                    f"same topic and format family ({family(r.video_format)}), "
                    f"title similarity {sim:.2f}",
                    round(sim, 3),
                )
            elif (
                same_objective(r, k)
                and r.target_sub_intent == k.target_sub_intent
                and (VideoFormat(r.video_format) == VideoFormat(k.video_format))
            ):
                dup = Merge(
                    k.recommendation_id,
                    r.recommendation_id,
                    "same topic, intent and format",
                    round(sim, 3),
                )
            if dup:
                break
        if dup:
            merges.append(dup)
        else:
            kept.append(r)
    return merges
