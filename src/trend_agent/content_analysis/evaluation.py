"""Offline evaluation of the content-analysis engine on labelled synthetic cases.

Splits are chronological: every case of a split is analysed *as of* the split's date, so
evidence published later is invisible (and checked: ``future_evidence_leaks`` must be 0).
``dev`` was used while developing the rules, ``calibration`` only for the relevance
threshold sweep, and ``heldout`` is run once, after the other two were frozen.

Labels were written by the author of the rules, before running the cases, from the case
descriptions alone. They are **not** independent annotations, the entities and videos are
fictional, and the cases are few; the metrics describe behaviour on these fixtures and are
not estimates of real-world accuracy.

Metrics
-------
* intent accuracy (primary and sub-intent), entity accuracy;
* relevance: precision@K and nDCG@K of the engine's relevance ranking against graded labels
  (0-3), and binary precision/recall at the configured threshold (grade >= 2 = relevant);
* gaps: precision (a reported opportunity gap has an acceptable labelled type), recall, and
  the false-gap rate (an opportunity gap reported where the label says none or abstain);
* abstention precision/recall (INSUFFICIENT_EVIDENCE vs label ``abstain``) and policy
  blocking accuracy;
* unsupported-claim rate: published claims that are not VERIFIED/CORROBORATED, plus the
  number of scripted unsupported LLM statements that were published (must be 0);
* cost per analysis: simulated search requests, quota units, LLM tokens and wall time.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from trend_agent.content_analysis.config import LoadedContentConfig
from trend_agent.content_analysis.demand import from_statistics, manual
from trend_agent.content_analysis.llm_client import ScriptedAnalysisLLM
from trend_agent.content_analysis.orchestrator import AnalysisOutcome
from trend_agent.content_analysis.synthetic import _video, analyzer_for, inputs_for
from trend_agent.schemas.content_gap import OPPORTUNITY_GAPS
from trend_agent.schemas.content_opportunity import (
    OpportunityStatus,
    ReportClaimKind,
    ReportClaimStatus,
)
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.world import load_world

SPLITS = ("dev", "calibration", "heldout")
K = 5
SEARCH_UNITS = 100  # search.list cost in YouTube quota units


def load_split(path: Path) -> tuple[datetime, list[dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    as_of = data["as_of"]
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    return as_of, list(data["cases"])


def _ndcg(grades_in_rank_order: Sequence[int], all_grades: Sequence[int], k: int) -> float | None:
    ideal = sorted(all_grades, reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return None
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades_in_rank_order[:k]))
    return float(dcg / idcg)


def run_case(
    case: dict[str, Any],
    *,
    as_of: datetime,
    loaded: LoadedContentConfig,
    vloaded: LoadedVerificationConfig,
    root: Path,
) -> tuple[AnalysisOutcome, float]:
    world = load_world(root / "content_analysis" / "world.yaml", vloaded)
    query = case["query"]
    videos = [_video(v) for v in case.get("videos", ())]
    llm = ScriptedAnalysisLLM(case["llm"]) if case.get("llm") else None
    analyzer, _ = analyzer_for(world, loaded, vloaded, videos, query, llm=llm)
    inp = inputs_for(world, case["target"], category=case.get("category"))
    t = world.targets[case["target"]]
    if case.get("demand", "target") == "none":
        demand = manual(query, t.query_id, synthetic=True)
    else:
        demand = from_statistics(
            inp.stats,
            query=query,
            query_id=t.query_id,
            category=t.category,
            topic_cluster_id=t.topic_id,
        )
    inp = replace(inp, query=query, demand=demand, now=as_of)
    started = time.perf_counter()
    outcome = asyncio.run(analyzer.analyse(inp))
    return outcome, time.perf_counter() - started


def score_case(case: dict[str, Any], outcome: AnalysisOutcome, as_of: datetime) -> dict[str, Any]:
    rep = outcome.report
    lab = case["labels"]
    grades = {str(v["id"]): int(v.get("g", 0)) for v in case.get("videos", ())}
    future = {
        str(v["id"])
        for v in case.get("videos", ())
        if v.get("p") and f"{v['p']}T12:00:00+00:00" > as_of.isoformat()
    }
    seen = {v.video_id for v in outcome.videos}
    ranked = sorted(outcome.relevance, key=lambda a: (-a.score, a.video_id))
    ranked_grades = [grades.get(a.video_id, 0) for a in ranked]
    visible_grades = [g for vid, g in grades.items() if vid in seen]
    rel_pred = {a.video_id for a in outcome.relevance if a.relevant}
    rel_true = {vid for vid, g in grades.items() if g >= 2 and vid in seen}
    primary = rep.primary_gap.gap_type.value if rep.primary_gap else None
    reported_gap = (
        primary is not None
        and rep.primary_gap is not None
        and (rep.primary_gap.gap_type in OPPORTUNITY_GAPS)
    )
    want = lab.get("gap", "none")
    acceptable = {want, *lab.get("gap_ok", ())} - {"none", "abstain", "blocked"}
    published = [c for c in rep.claims if c.published]
    unsupported = [
        c
        for c in published
        if c.status not in (ReportClaimStatus.VERIFIED, ReportClaimStatus.CORROBORATED)
    ]
    scripted_bad = set(case.get("unsupported_llm_statements", ()))
    leaked_llm = [
        c.text
        for c in published
        if c.kind is ReportClaimKind.NARRATIVE and any(b in c.text for b in scripted_bad)
    ]
    entity = rep.entity
    want_entity = lab.get("entity")
    sub = rep.intent.sub_intent.value if rep.intent.sub_intent else None
    return {
        "case": case["id"],
        "query": case["query"],
        "intent_ok": rep.intent.primary_intent.value == lab["primary_intent"],
        "sub_intent_ok": None if "sub_intent" not in lab else sub == lab["sub_intent"],
        "entity_ok": (entity is None)
        if want_entity in (None, "none", "ambiguous")
        else entity == want_entity,
        "p_at_k": (
            sum(g >= 2 for g in ranked_grades[:K]) / min(K, len(ranked_grades))
            if ranked_grades
            else None
        ),
        "ndcg_at_k": _ndcg(ranked_grades, visible_grades, K),
        "rel_tp": len(rel_pred & rel_true),
        "rel_fp": len(rel_pred - rel_true),
        "rel_fn": len(rel_true - rel_pred),
        "label_gap": want,
        "predicted_gap": primary,
        "predicted_status": rep.status.value,
        "reported_gap": reported_gap,
        "gap_correct": reported_gap and primary in acceptable,
        "abstained": rep.status is OpportunityStatus.INSUFFICIENT_EVIDENCE,
        "blocked": rep.status is OpportunityStatus.BLOCKED_POLICY,
        "published_claims": len(published),
        "unsupported_published": len(unsupported),
        "llm_unsupported_published": len(leaked_llm),
        "future_evidence_leaks": len(future & seen),
        "search_requests": outcome.cost.search_requests,
        "quota_units": outcome.cost.search_requests * SEARCH_UNITS,
        "llm_tokens": outcome.cost.llm_input_tokens + outcome.cost.llm_output_tokens,
        "score": rep.score.score,
    }


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 3) if den else None


def aggregate(rows: Sequence[dict[str, Any]], seconds: Sequence[float]) -> dict[str, Any]:
    n = len(rows)

    def mean(key: str) -> float | None:
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    reported = [r for r in rows if r["reported_gap"]]
    true_gap = [r for r in rows if r["label_gap"] not in ("none", "abstain", "blocked")]
    no_gap = [r for r in rows if r["label_gap"] in ("none", "abstain")]
    abstain_label = [r for r in rows if r["label_gap"] == "abstain"]
    abstained = [r for r in rows if r["abstained"]]
    blocked_label = [r for r in rows if r["label_gap"] == "blocked"]
    tp = sum(r["rel_tp"] for r in rows)
    fp = sum(r["rel_fp"] for r in rows)
    fn = sum(r["rel_fn"] for r in rows)
    published = sum(r["published_claims"] for r in rows)
    subs = [r for r in rows if r["sub_intent_ok"] is not None]
    return {
        "cases": n,
        "intent_accuracy": _ratio(sum(r["intent_ok"] for r in rows), n),
        "sub_intent_accuracy": _ratio(sum(bool(r["sub_intent_ok"]) for r in subs), len(subs)),
        "entity_accuracy": _ratio(sum(r["entity_ok"] for r in rows), n),
        f"relevance_precision_at_{K}": mean("p_at_k"),
        f"relevance_ndcg_at_{K}": mean("ndcg_at_k"),
        "relevant_precision": _ratio(tp, tp + fp),
        "relevant_recall": _ratio(tp, tp + fn),
        "gap_precision": _ratio(sum(r["gap_correct"] for r in reported), len(reported)),
        "gap_recall": _ratio(sum(r["gap_correct"] for r in true_gap), len(true_gap)),
        "false_gap_rate": _ratio(sum(r["reported_gap"] for r in no_gap), len(no_gap)),
        "abstention_precision": _ratio(
            sum(r["label_gap"] == "abstain" for r in abstained), len(abstained)
        ),
        "abstention_recall": _ratio(sum(r["abstained"] for r in abstain_label), len(abstain_label)),
        "policy_block_accuracy": _ratio(
            sum(r["blocked"] for r in blocked_label), len(blocked_label)
        ),
        "unsupported_published_claim_rate": _ratio(
            sum(r["unsupported_published"] for r in rows), published
        ),
        "llm_unsupported_statements_published": sum(r["llm_unsupported_published"] for r in rows),
        "future_evidence_leaks": sum(r["future_evidence_leaks"] for r in rows),
        "mean_search_requests": mean("search_requests"),
        "mean_quota_units": mean("quota_units"),
        "mean_llm_tokens": mean("llm_tokens"),
        "mean_seconds": round(sum(seconds) / len(seconds), 4) if seconds else None,
    }


def evaluate(
    loaded: LoadedContentConfig,
    vloaded: LoadedVerificationConfig,
    root: Path,
    *,
    splits: Sequence[str] = SPLITS,
) -> dict[str, Any]:
    out: dict[str, Any] = {"splits": {}, "rows": {}, "failures": []}
    for split in splits:
        as_of, cases = load_split(root / "content_analysis" / f"eval_{split}.yaml")
        rows, secs = [], []
        for case in cases:
            outcome, sec = run_case(case, as_of=as_of, loaded=loaded, vloaded=vloaded, root=root)
            row = score_case(case, outcome, as_of)
            rows.append(row)
            secs.append(sec)
            problems = [k for k in ("intent_ok", "entity_ok") if not row[k]]
            if row["sub_intent_ok"] is False:
                problems.append("sub_intent")
            want = row["label_gap"]
            if row["abstained"] and want not in ("abstain", "blocked"):
                problems.append(f"unexpected abstention (label {want})")
            elif want == "abstain" and not row["abstained"]:
                problems.append(f"expected abstention, got {row['predicted_status']}")
            elif want == "blocked" and not row["blocked"]:
                problems.append("expected policy block")
            elif want == "none" and row["reported_gap"]:
                problems.append(f"false gap {row['predicted_gap']}")
            elif want not in ("none", "abstain", "blocked") and not row["gap_correct"]:
                problems.append(f"expected gap {want}, got {row['predicted_gap']}")
            if problems:
                out["failures"].append({"split": split, "case": row["case"], "problems": problems})
        out["splits"][split] = {"as_of": as_of.isoformat(), **aggregate(rows, secs)}
        out["rows"][split] = rows
    out["notice"] = (
        "Synthetic, fictional cases labelled by the rules' author (not independent "
        "annotators). These numbers describe behaviour on fixtures, not real-world accuracy."
    )
    return out


def threshold_sweep(
    loaded: LoadedContentConfig,
    vloaded: LoadedVerificationConfig,
    root: Path,
    thresholds: Sequence[float] = (30, 35, 40, 45, 50, 55, 60),
) -> list[dict[str, Any]]:
    """Binary relevance precision/recall on the calibration split per relevance threshold."""
    out = []
    for th in thresholds:
        cfg = loaded.config.model_copy(
            update={
                "relevance": loaded.config.relevance.model_copy(
                    update={"relevant_threshold": float(th)}
                )
            }
        )
        lc = loaded.model_copy(update={"config": cfg})
        res = evaluate(lc, vloaded, root, splits=("calibration",))
        agg = res["splits"]["calibration"]
        p, r = agg["relevant_precision"], agg["relevant_recall"]
        f1 = round(2 * p * r / (p + r), 3) if p and r else None
        out.append(
            {
                "threshold": th,
                "precision": p,
                "recall": r,
                "f1": f1,
                "false_gap_rate": agg["false_gap_rate"],
                "gap_precision": agg["gap_precision"],
            }
        )
    return out


def to_json(results: dict[str, Any]) -> str:
    return json.dumps(results, indent=2, default=str)
