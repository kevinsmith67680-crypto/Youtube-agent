"""Conservative content freshness.

Freshness depends on intent. Evergreen intents (reviews, strategy, programming concepts)
are not penalised for age. For time-sensitive intents:

* ``OUTDATED_WITH_EVIDENCE`` requires a *verified* change that materially affects the
  video: the video is about an older version/season **and** a credible record states a
  breaking change, removal or a new season/version that replaces it;
* ``POTENTIALLY_OUTDATED`` - about an older version without such a record, or published
  before a verified release/change (it may predate the change; coverage is unknown);
* ``CURRENT`` - consistent with the verified state, or recent with no verified change;
* ``UNKNOWN`` - no publication date, or old with no verified change (age alone never
  establishes that a video is outdated or inaccurate).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.content_analysis.entity_verification import ChangeEvent, EntityFacts
from trend_agent.schemas.search_intent import IntentClassification
from trend_agent.schemas.search_result import (
    CandidateVideo,
    EntityMatch,
    FreshnessAssessment,
    FreshnessStatus,
    RelevanceAssessment,
)


def assess(
    video: CandidateVideo,
    relevance: RelevanceAssessment,
    intent: IntentClassification,
    facts: EntityFacts | None,
    config: ContentAnalysisConfig,
    *,
    now: datetime,
) -> FreshnessAssessment:
    ts = intent.time_sensitive

    def out(
        status: FreshnessStatus,
        reason: str,
        ref: ChangeEvent | None = None,
        ref_date: date | None = None,
        evidence: tuple[str, ...] = (),
    ) -> FreshnessAssessment:
        when = ref.when if ref else ref_date
        return FreshnessAssessment(
            video_id=video.video_id,
            status=status,
            reason=reason,
            reference_event=ref.description if ref else None,
            reference_date=datetime.combine(when, datetime.min.time(), now.tzinfo)
            if when
            else None,
            evidence_ids=(ref.evidence_id,) if ref else evidence,
            time_sensitive=ts,
        )

    if video.published_at is None:
        return out(FreshnessStatus.UNKNOWN, "no publication date")
    change = facts.latest_change if facts else None
    breaking = [c for c in (facts.changes if facts else ()) if c.breaking]
    ref_date = (change.when if change else None) or (facts.release_date if facts else None)
    published = video.published_at.date()
    if relevance.entity_match is EntityMatch.VERSION_MISMATCH and facts is not None:
        current = facts.versioned.version
        older = ", ".join(relevance.matched_versions)
        # Obsolete only with a verified breaking change (removal, replacement, vaulting).
        ref = breaking[-1] if breaking else None
        if ref is not None:
            return out(
                FreshnessStatus.OUTDATED_WITH_EVIDENCE,
                f"about {older} while a credible record dated {ref.when.isoformat()} "
                f"establishes {current or 'a newer version'}"
                + (" with breaking changes" if ref.breaking else ""),
                ref,
            )
        return out(
            FreshnessStatus.POTENTIALLY_OUTDATED,
            f"about {older}, not {current}; no verified breaking change is recorded",
        )
    if not ts:
        return out(FreshnessStatus.CURRENT, "evergreen intent: age alone does not make it outdated")
    if (
        ref_date is not None
        and published < ref_date
        and relevance.entity_match in (EntityMatch.FAMILY, EntityMatch.EXACT, EntityMatch.NONE)
    ):
        what = "change" if change else "release"
        return out(
            FreshnessStatus.POTENTIALLY_OUTDATED,
            f"published {published.isoformat()}, before the verified {what} on "
            f"{ref_date.isoformat()}; whether it reflects the {what} is not established",
            change,
            ref_date,
            facts.ref.evidence_ids[:1] if facts else (),
        )
    window = config.freshness.recent_window.get(intent.category or "", timedelta(days=60))
    if ref_date is not None:
        return out(FreshnessStatus.CURRENT, "published after the latest verified change")
    if now - video.published_at <= window:
        return out(FreshnessStatus.CURRENT, "recent, and no verified change is known")
    return out(
        FreshnessStatus.UNKNOWN,
        "older than the recent window, but no verified change shows it is outdated",
    )
