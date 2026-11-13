from __future__ import annotations

from tests.unit.content_analysis.helpers import analyse, v
from trend_agent.schemas.search_result import (
    CoverageStatus,
    EntityMatch,
    EvidenceTier,
    FreshnessStatus,
)

PYXIS = [
    v("p1", "Pyxis 4 tutorial for beginners", "Build an app with Pyxis 4 and the router API.",
      p="2025-11-01"),
    v("p2", "Pyxis 5 migration guide", "Migrate from Pyxis 4 to Pyxis 5 step by step.",
      p="2026-10-30"),
    v("p3", "Cooking pasta", "Recipe.", p="2026-10-01"),
    *[v(f"p{i}", f"Pyxis 4 tips part {i}", "Pyxis 4 tips.", p="2026-02-01") for i in range(4, 10)],
]  # fmt: skip


def test_version_mismatch_and_outdated_needs_a_verified_breaking_change() -> None:
    hard = analyse("pyxis", PYXIS)
    rel = {a.video_id: a for a in hard.relevance}
    fresh = {f.video_id: f for f in hard.freshness}
    assert rel["p1"].entity_match is EntityMatch.VERSION_MISMATCH
    assert not rel["p1"].relevant
    assert rel["p2"].relevant
    assert not rel["p3"].relevant
    assert fresh["p1"].status is FreshnessStatus.OUTDATED_WITH_EVIDENCE
    assert fresh["p1"].evidence_ids  # cites the breaking-change record
    soft = analyse("pyxis_soft", PYXIS)
    fresh_soft = {f.video_id: f for f in soft.freshness}
    # Same videos, no breaking-change record: never "outdated", at most "potentially".
    assert fresh_soft["p1"].status is FreshnessStatus.POTENTIALLY_OUTDATED
    assert all(f.status is not FreshnessStatus.OUTDATED_WITH_EVIDENCE for f in soft.freshness)


def test_age_alone_never_makes_an_evergreen_video_outdated() -> None:
    out = analyse(
        "quanta",
        [v(f"q{i}", f"Quanta Notes 3 review {i}", "Hands-on review.", p="2026-03-05")
         for i in range(8)],
        query="quanta notes 3 review",
    )  # fmt: skip
    assert all(f.status is FreshnessStatus.CURRENT for f in out.freshness)


def test_homonym_is_another_entity_not_a_match() -> None:
    out = analyse(
        "orbit",
        [v("o1", "Orbit smart glasses setup", "Set up the Orbit smart glasses."),
         v("o2", "Space orbit explained", "How orbits work.")],
        query="orbit model api tutorial", category="ai",
    )  # fmt: skip
    rel = {a.video_id: a for a in out.relevance}
    assert rel["o1"].entity_match is EntityMatch.OTHER_ENTITY
    assert not rel["o1"].relevant


def test_metadata_only_coverage_is_scope_limited() -> None:
    out = analyse("pyxis", PYXIS)
    assert all(c.evidence_tier is EvidenceTier.A_METADATA for c in out.cells)
    statuses = {c.status for c in out.cells}
    assert CoverageStatus.NOT_EVIDENCED in statuses
    for s in out.summaries:
        if s.best_status is CoverageStatus.NOT_EVIDENCED:
            assert "does not show" in s.statement or "not established" in s.statement


def test_supply_never_uses_view_counts_or_total_results() -> None:
    out = analyse("pyxis", PYXIS)
    sup = out.report.supply
    text = " ".join((sup.scope, *sup.statements))
    assert sup.scope.startswith("Of the")
    assert "views" not in text.lower()
    assert (
        sup.api_total_estimate is None
        or "not a count" in type(sup).model_fields["api_total_estimate"].description
    )
