"""The Phase 7 evaluation framework (labelled drafts, duplicate sweep, generated ideas)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tests.helpers import ROOT
from trend_agent import cli
from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.evaluation import (
    evaluate,
    grounded,
    load_split,
    sweep_duplicates,
)

CONFIG = ROOT / "config"


def test_splits_are_disjoint_and_fully_labelled() -> None:
    cal, ho = load_split(CONFIG, "calibration"), load_split(CONFIG, "heldout")
    assert {d["id"] for d in cal["drafts"]}.isdisjoint({d["id"] for d in ho["drafts"]})
    cal_texts = {i["text"] for d in cal["drafts"] for i in d["items"]}
    ho_bad = {i["text"] for d in ho["drafts"] for i in d["items"] if i["gold"] != "plan"}
    assert (
        not (ho_bad - {"Pyxis 5 is released.", "Skyforge Arena Season 8: Emberfall is live."})
        & cal_texts
    )
    for split in (cal, ho):
        for d in split["drafts"]:
            assert d["acceptable_status"]
            for i in d["items"]:
                assert i["gold"] in ("supported", "unsupported", "result_as_fact", "unsafe", "plan")
            for t in [*d["titles"], *d["hooks"]]:
                assert t["gold"] in ("ok", "reject")


def test_grounding_checker_is_strict() -> None:
    src = ["CodeForge 2 is available today for Windows, macOS and Linux."]
    assert grounded("CodeForge 2 is available for Windows, macOS and Linux.", src)
    assert not grounded("CodeForge 2 is 40% faster than CodeForge 1.", src)
    assert not grounded("CodeForge 2 is available for Windows.", [])
    claim = "According to Lumen AI: CodeForge 2 is available today for Windows, macOS and Linux."
    assert grounded(claim, src, ("Lumen AI",))
    assert not grounded(claim, src, ("Someone Else",))


def test_duplicate_sweep_finds_a_threshold() -> None:
    pairs = [
        {
            "a": "How to Install X 2: Setup Guide",
            "b": "How to Install X 2 Setup Guide",
            "duplicate": True,
        },
        {"a": "How to Install X 2: Setup Guide", "b": "X 2 Review", "duplicate": False},
    ]
    s = sweep_duplicates(pairs)
    assert s["best_f1"] == 1.0
    assert 0.2 <= s["best_threshold"] <= 0.9


def test_evaluation_on_both_splits(rec_loaded: LoadedRecommendationConfig) -> None:
    rep = evaluate(CONFIG, rec_loaded).as_dict()
    for name in ("calibration", "heldout"):
        s = rep["splits"][name]
        assert s["cases"] == 8
        assert s["false_approvals"] == []
        assert s["unsupported_published_as_fact"] == []
        assert s["supported_fact_retention"] == 1.0
    g = rep["generated"]
    assert g["factual_grounding"]["unsupported_claim_rate"] == 0.0
    assert g["search_intent_alignment"]["share_serving_intent"] == 1.0
    assert g["diversity_top10"]["max_pairwise_title_jaccard"] < 0.6
    assert rep["human_usefulness"]["measured"] is False
    assert "not evidence of real-world" in rep["notice"]


def test_human_labels_are_used_when_given(
    rec_loaded: LoadedRecommendationConfig, tmp_path: Path
) -> None:
    labels = tmp_path / "labels.yaml"
    labels.write_text("a: true\nb: false\nc: true\n", "utf-8")
    rep = evaluate(CONFIG, rec_loaded, splits=("heldout",), human_labels=labels).as_dict()
    assert rep["human_usefulness"]["labelled"] == 3
    assert rep["human_usefulness"]["useful_rate"] == 0.667


def test_cli_evaluate(tmp_path: Path) -> None:
    out = tmp_path / "eval.json"
    r = CliRunner().invoke(
        cli.app, ["recommend", "evaluate", "--split", "heldout", "--out", str(out)]
    )
    assert r.exit_code == 0, r.output
    assert "not evidence of real-world" in " ".join(r.stdout.split())
    data = json.loads(out.read_text())
    assert list(data["splits"]) == ["heldout"]
    assert CliRunner().invoke(cli.app, ["recommend", "evaluate", "--split", "nope"]).exit_code == 2
