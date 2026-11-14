"""Recommendation quality: offline rules evaluation plus (descriptive) real outcomes.

The offline evaluation (Phase 7 fixtures, deterministic generator, no LLM judge) measures
status accuracy, false approvals, unsupported statements published as fact, duplicate
detection and grounding. Ideas are judged by rules and author labels, never by the model
that wrote them.

Real outcomes are creator-entered (``recommendation_outcomes``) and summarised
descriptively. They are NOT used to claim that a recommendation caused views: there is no
control group, and higher search interest does not guarantee more views.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from trend_agent.db.repositories.ops import OutcomeRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.recommendations.config import load_recommendation_config
from trend_agent.recommendations.evaluation import SPLITS
from trend_agent.recommendations.evaluation import evaluate as phase7_evaluate


def evaluate_offline(root: Path) -> dict[str, Any]:
    rep = phase7_evaluate(root, load_recommendation_config(root), splits=SPLITS).as_dict()
    held = rep["splits"].get("heldout", {})
    dup = rep.get("duplicates", {}).get("heldout", {}).get("configured", {})
    gen = rep.get("generated", {})
    grounding = gen.get("factual_grounding", {})
    return {
        "origin": "synthetic",
        "judge": "deterministic rules and author labels (no LLM self-judgement)",
        "heldout": {
            "cases": held.get("cases"),
            "status_accuracy": held.get("status_accuracy"),
            "false_approvals": len(held.get("false_approvals", [])),
            "unsupported_published_as_fact": len(held.get("unsupported_published_as_fact", [])),
            "bad_statement_removal": held.get("bad_statement_removal"),
            "supported_fact_retention": held.get("supported_fact_retention"),
        },
        "duplicate_f1": dup.get("f1"),
        "grounding_violation_rate": grounding.get("unsupported_claim_rate"),
        "generated_ideas": gen.get("recommendations"),
        "diversity_top10_max_title_jaccard": gen.get("diversity_top10", {}).get(
            "max_pairwise_title_jaccard"
        ),
        "full_report_splits": list(rep["splits"]),
    }


def outcome_summary(factory: SessionFactory | None) -> dict[str, Any]:
    if factory is None:
        return {"status": "UNKNOWN", "reason": "no database"}
    with transaction(factory) as s:
        rows = OutcomeRepository(s).all()
        data = [(r.days_since_publication, r.views, r.search_traffic_share) for r in rows]
    if not data:
        return {
            "status": "UNKNOWN",
            "outcomes": 0,
            "reason": "no creator-entered outcomes recorded",
        }
    views = [v for _, v, _ in data if v is not None]
    shares = [s for _, _, s in data if s is not None]
    return {
        "status": "DESCRIPTIVE",
        "outcomes": len(data),
        "median_views": statistics.median(views) if views else None,
        "median_search_traffic_share": statistics.median(shares) if shares else None,
        "caveat": "descriptive only: no control group, so no causal claim about the value of "
        "a recommendation is made",
    }
