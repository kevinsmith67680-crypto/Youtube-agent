from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.content_analysis.helpers import analyse, v
from trend_agent.content_analysis.demand import basis_for
from trend_agent.schemas.content_gap import GapType, GapVerification
from trend_agent.schemas.content_opportunity import DemandBasis, OpportunityStatus
from trend_agent.schemas.detection import ColdStartState
from trend_agent.schemas.evidence import AlertStage, SurgeClassification

C, S = SurgeClassification, AlertStage


def _alert(cls: SurgeClassification, stage: AlertStage, cold: Any = None) -> Any:
    return SimpleNamespace(classification=cls, alert_status=stage, cold_start_state=cold)


@pytest.mark.parametrize(
    ("cls", "stage", "cold", "basis"),
    [
        (C.DIRECT_SEARCH_SURGE, S.CONFIRMED, None,
         DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE),
        (C.DIRECT_SEARCH_SURGE, S.CANDIDATE, None, DemandBasis.DIRECT_SEARCH_CANDIDATE),
        (C.EXTERNAL_SEARCH_SURGE, S.CONFIRMED, None, DemandBasis.EXTERNAL_SEARCH_SURGE),
        (C.PROXY_ATTENTION_SIGNAL, S.CONFIRMED, None,
         DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY),
        (C.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY, S.CANDIDATE, None,
         DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY),
        (C.DIRECT_SEARCH_SURGE, S.WATCH, None, DemandBasis.WATCH),
        (C.DIRECT_SEARCH_SURGE, S.CANDIDATE, ColdStartState.COLD_START_GROWTH_CANDIDATE,
         DemandBasis.COLD_START_GROWTH_CANDIDATE),
        (C.DIRECT_SEARCH_SURGE, S.INSUFFICIENT_EVIDENCE, None, DemandBasis.INSUFFICIENT_EVIDENCE),
    ],
)  # fmt: skip
def test_phase4_classification_maps_to_a_label_without_upgrading(
    cls: SurgeClassification, stage: AlertStage, cold: Any, basis: DemandBasis
) -> None:
    assert basis_for(_alert(cls, stage, cold)) is basis


def test_no_alert_is_insufficient_evidence() -> None:
    assert basis_for(None) is DemandBasis.INSUFFICIENT_EVIDENCE


def test_external_surge_is_never_described_as_youtube_search_growth() -> None:
    out = analyse("pyxis", [v(f"x{i}", f"Pyxis 4 tip {i}", p="2026-02-01") for i in range(9)])
    d = out.report.demand
    assert d.basis is DemandBasis.EXTERNAL_SEARCH_SURGE
    assert out.report.score.cap is not None
    assert out.report.score.cap <= 70
    assert "youtube search" not in out.report.explanation["why_investigate"].lower() or (
        "not" in out.report.explanation["why_investigate"].lower()
    )


def test_manual_analysis_is_labelled_and_capped() -> None:
    out = analyse("helios", [v(f"h{i}", "Helios Agent news", "News.") for i in range(9)],
                  no_demand=True)  # fmt: skip
    assert out.report.demand.basis is DemandBasis.MANUAL_NO_DEMAND_EVIDENCE
    assert out.report.score.score is None or out.report.score.score <= 50


def test_too_few_results_abstain_and_have_no_score() -> None:
    out = analyse("helios", [v("h1", "Helios Agent tutorial", "Step by step.")])
    assert out.report.status is OpportunityStatus.INSUFFICIENT_EVIDENCE
    assert out.report.score.score is None
    assert [g.gap_type for g in out.gaps] == [GapType.INSUFFICIENT_EVIDENCE]


def test_adequate_coverage_yields_no_verifiable_gap_only() -> None:
    vids = [
        v(f"q{i}", f"Quanta Notes 3 tutorial part {i}",
          "Learn Quanta Notes 3 step by step: setup, linked notes and templates.", p="2026-06-01")
        for i in range(8)
    ]  # fmt: skip
    out = analyse("quanta", vids)
    assert [g.gap_type for g in out.gaps] == [GapType.NO_VERIFIABLE_GAP]
    assert out.report.status is OpportunityStatus.NO_OPPORTUNITY


def test_unknown_product_version_is_not_an_opportunity() -> None:
    vids = [v(f"c{i}", f"CodeForge 3 rumour {i}", "Speculation.") for i in range(9)]
    out = analyse("codeforge", vids, query="codeforge 3 release date", no_demand=True)
    assert out.report.status is OpportunityStatus.INSUFFICIENT_EVIDENCE
    assert "codeforge 3" in out.gaps[0].statement.lower()


def test_vaulted_item_gets_no_location_opportunity() -> None:
    vids = [v(f"a{i}", f"Skyforge Arena Season 7 Aurora Glider location {i}",
              "Find the Aurora Glider.", p="2026-09-20") for i in range(9)]  # fmt: skip
    out = analyse("skyforge", vids, query="skyforge arena season 8 aurora glider location")
    assert out.report.status is OpportunityStatus.INSUFFICIENT_EVIDENCE
    assert "vaulted" in out.gaps[0].statement
    # The added item in the same announcement is not affected.
    ok = analyse("skyforge", vids, query="where to find the ember lantern skyforge arena season 8")
    assert ok.report.status is not OpportunityStatus.INSUFFICIENT_EVIDENCE


def test_missing_components_are_excluded_not_zero() -> None:
    out = analyse("helios", [v(f"h{i}", "Helios Agent news", "News.") for i in range(9)])
    sc = out.report.score
    for comp in sc.components:
        if comp.value is None:
            assert comp.contribution is None
    assert sc.available_weight <= 1.0
    assert "not a probability" in sc.note


def test_metadata_only_specific_answer_gaps_are_weak_hypotheses() -> None:
    vids = [
        v("f1", "Helios Agent tutorial: overview", ""),
        *[v(f"f{i}", f"Helios Agent tutorial part {i}", "") for i in range(2, 10)],
    ]
    out = analyse("helios", vids)
    for g in out.gaps:
        if g.gap_type is GapType.MISSING_SPECIFIC_ANSWER:
            assert g.verification_status is GapVerification.HYPOTHESIS
            assert any("titles and descriptions" in lim for lim in g.limitations)
