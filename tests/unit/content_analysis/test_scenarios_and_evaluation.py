"""Synthetic scenarios A-J and the labelled evaluation as regression guards."""

from __future__ import annotations

import pytest

from tests.unit.content_analysis.helpers import LOADED, VLOADED
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent.content_analysis.evaluation import evaluate
from trend_agent.content_analysis.synthetic import load_scenarios, run_scenario

SCENARIOS = load_scenarios(CONFIG_DIR)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_meets_its_expectations(scenario: dict[str, object]) -> None:
    result = run_scenario(scenario, LOADED, VLOADED, CONFIG_DIR)
    failed = [(n, d) for n, ok, d in result.checks if not ok]
    assert not failed, failed
    rep = result.outcome.report
    assert rep.synthetic
    assert rep.score.score is None or 0 <= rep.score.score <= 100


def test_scenarios_cover_a_to_j() -> None:
    ids = {s["id"] for s in SCENARIOS}
    assert {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J"} <= ids


def test_evaluation_safety_invariants_hold_on_every_split() -> None:
    res = evaluate(LOADED, VLOADED, CONFIG_DIR)
    for split, agg in res["splits"].items():
        assert agg["future_evidence_leaks"] == 0, split
        assert agg["unsupported_published_claim_rate"] in (0.0, None), split
        assert agg["llm_unsupported_statements_published"] == 0, split
        assert agg["false_gap_rate"] in (0.0, None), split
        assert agg["policy_block_accuracy"] in (1.0, None), split
    assert "not real-world accuracy" in res["notice"]
