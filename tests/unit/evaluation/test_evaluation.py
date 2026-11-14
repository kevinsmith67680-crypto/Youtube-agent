"""Phase 8 evaluation helpers: label validation, gap classes, real-label scoring, backtest
validity, the quality scorecard and the bounded detection evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trend_agent.evaluation import scorecard
from trend_agent.evaluation.content_gaps import label_class, predicted_class
from trend_agent.evaluation.detection import score_real
from trend_agent.evaluation.labels import validate
from trend_agent.evaluation.replay import classify
from trend_agent.schemas.content_gap import GapVerification
from trend_agent.schemas.content_opportunity import OpportunityStatus

T = datetime(2026, 10, 1, tzinfo=UTC)
CONFIG = Path(__file__).resolve().parents[3] / "config"


def _line(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "surge_event",
        "subject_key": "target:42",
        "event_time": "2026-10-01T00:00:00Z",
        "known_at": "2026-10-08T00:00:00Z",
        "label": {"kind": "spike"},
        "source": "manual review",
        "labeler": "analyst-1",
    }
    return base | over


def test_label_validation_accepts_provenanced_labels() -> None:
    v = validate(_line(), "real")
    assert v["event_time"] == T
    assert v["known_at"] > v["event_time"]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"known_at": "2026-09-01T00:00:00Z"}, "ground truth from the future"),
        ({"source": ""}, "source is required"),
        ({"labeler": None}, "labeler is required"),
        ({"kind": "vibes"}, "unknown kind"),
        ({"subject_key": "synthetic:demo-1"}, "synthetic subject"),
        ({"origin": "synthetic"}, "!= file origin"),
        ({"event_time": "2026-10-01T00:00:00"}, "UTC offset"),
    ],
)
def test_label_validation_rejects(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate(_line(**over), "real")


def test_synthetic_subjects_are_fine_in_synthetic_files() -> None:
    assert validate(_line(subject_key="synthetic:demo-1", origin="synthetic"), "synthetic")


def _report(status: OpportunityStatus, verification: GapVerification | None) -> Any:
    gap = None if verification is None else SimpleNamespace(verification_status=verification)
    return SimpleNamespace(status=status, primary_gap=gap)


def test_four_gap_classes() -> None:
    assert predicted_class(_report(OpportunityStatus.CANDIDATE, GapVerification.VERIFIED)) == (
        "VERIFIED_GAP"
    )
    assert predicted_class(_report(OpportunityStatus.CANDIDATE, GapVerification.HYPOTHESIS)) == (
        "CANDIDATE_GAP"
    )
    assert predicted_class(_report(OpportunityStatus.NO_OPPORTUNITY, None)) == "NO_VERIFIABLE_GAP"
    assert predicted_class(_report(OpportunityStatus.INSUFFICIENT_EVIDENCE, None)) == (
        "INSUFFICIENT_EVIDENCE"
    )
    assert predicted_class(_report(OpportunityStatus.BLOCKED_POLICY, None)) == "BLOCKED_POLICY"
    assert label_class("none") == "NO_GAP"
    assert label_class("abstain") == "INSUFFICIENT_EVIDENCE"
    assert label_class("version_mismatch") == "GAP"


def test_real_detection_is_unknown_below_the_minimum() -> None:
    res = score_real([{"subject_key": "a", "event_time": T}] * 5, [], min_labels=30)
    assert res["status"] == "UNKNOWN"
    assert "not enough" in res["reason"]


def test_real_detection_scoring() -> None:
    labels = [{"subject_key": f"t{i}", "event_time": T} for i in range(4)]
    alerts = [
        {"subject_key": "t0", "alerted_at": T + timedelta(days=1)},
        {"subject_key": "t1", "alerted_at": T + timedelta(days=10)},  # too late
        {"subject_key": "t9", "alerted_at": T},  # no such event
    ]
    res = score_real(labels, alerts, min_labels=2)
    assert res["status"] == "MEASURED"
    assert res["recall"] == 0.25
    assert res["precision"] == round(1 / 3, 4)
    assert res["delay_median_days"] == 1.0


def test_backtest_validity_classes() -> None:
    assert classify(10, 0, 0) == "VALID"
    assert classify(10, 3, 0) == "LIMITED"
    assert classify(10, 8, 0) == "INVALID"
    assert classify(10, 0, 1) == "INVALID"
    assert classify(0, 0, 0) == "INVALID"


def test_scorecard_keeps_real_world_unknown_without_labels() -> None:
    results = {
        "detection": {
            "current": {
                "test": {"precision": 0.9, "recall": 0.95, "false_alerts_per_1000_series_day": 0.4}
            }
        },
        "content_gaps": {"splits": {"heldout": {"false_gap_rate": 0.5, "false_verified_gaps": 0}}},
    }
    card = scorecard.build(results, {})
    assert card["components"]["detection"]["offline"] == "MEETS_TARGET"
    assert card["components"]["content_gaps"]["offline"] == "BELOW_TARGET"
    assert card["components"]["forecasting"]["offline"] == "NOT_RUN"
    assert card["real_world_accuracy"] == "UNKNOWN"
    assert all(c["real_world"]["status"] == "UNKNOWN" for c in card["components"].values())
    assert "not evidence of real-world accuracy" in card["notice"]


def test_tiny_detection_evaluation_reports_splits_and_sources() -> None:
    from trend_agent.detection.config import load_detection_config
    from trend_agent.evaluation.detection import evaluate_synthetic

    rep = evaluate_synthetic(load_detection_config(CONFIG), now=T, size="tiny")
    assert rep["origin"] == "synthetic"
    assert rep["splits"]["test"]["used_for_tuning"] is False
    assert set(rep["by_source_test"]) == {"direct_youtube_search_interest", "video_activity"}
    proxy = rep["by_source_test"]["video_activity"]
    # a proxy source can never produce a CONFIRMED (search-demand) alert
    assert proxy["confirmed_recall"] in (0.0, None)
    assert rep["current"]["test"]["labels"] > 0
