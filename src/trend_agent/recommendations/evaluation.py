"""Offline evaluation of the Phase 7 recommendation engine.

Two kinds of evidence are combined:

1. **Labelled drafts** (``config/recommendations_eval/eval_{calibration,heldout}.yaml``):
   hand-written concept drafts over the Phase 5 synthetic scenarios, with a gold label for
   every title, hook and outline statement and the statuses a careful reviewer would accept.
   They exercise the verification rules with deliberate failures (fabricated facts, invented
   numbers and versions, untested results written as done, fake citations, clickbait,
   urgency, forecast/view claims, prompt injection, shell commands).
   * ``calibration`` was used to inspect behaviour and to sweep the duplicate-title
     threshold; ``heldout`` was written separately and is reported with the rules frozen.
2. **Generated recommendations** from the deterministic template generator over the same
   scenarios, measured with checks that are independent of the verification gate: a
   lexical grounding check of every VERIFIED_FACT against the text of the records it cites,
   format/intent alignment from the format catalogue, gap fit, feasibility bounds and
   pairwise title similarity.

The labels were written by the author of the rules, not by independent annotators, the
entities are fictional and the cases are few. **Nothing here is an estimate of real-world
recommendation accuracy or usefulness.** Human usefulness needs creator judgements: it is
reported from a labels file when one exists (and from recorded feedback in production via
``recommend stats``), otherwise as not measured. No LLM grades its own output.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from trend_agent.recommendations import diversity
from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import EvidenceContext
from trend_agent.recommendations.deduplication import jaccard, normalized
from trend_agent.recommendations.format_selector import _GAP_FIT, CATALOG, intent_alignment
from trend_agent.recommendations.idea_generator import ConceptDraft
from trend_agent.recommendations.orchestrator import RecommendationEngine
from trend_agent.recommendations.verification import verify_concept
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    RecommendationOutput,
    StatementLabel,
    VideoFormat,
)

SPLITS = ("calibration", "heldout")
_DEFAULT_FORMAT = {"A": "LOCAL_AI_INSTALLATION_GUIDE", "C": "SOFTWARE_MIGRATION_GUIDE"}
_BAD = ("unsupported", "result_as_fact", "unsafe")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the and or of in on to for is are was were be by with as at it its this that "
    "from into than then".split()
)


@dataclass
class Fixture:
    report: Any
    evidence: EvidenceContext | None


def load_fixtures(
    root: Path, keys: tuple[str, ...] = ("A", "B", "C", "G", "J")
) -> dict[str, Fixture]:
    """Phase 5 synthetic scenarios analysed with the deterministic analyser."""
    from trend_agent.content_analysis import synthetic as ca_syn
    from trend_agent.content_analysis.config import load_content_config
    from trend_agent.verification.config import load_verification_config
    from trend_agent.verification.world import load_world

    lc, vc = load_content_config(root), load_verification_config(root)
    world = load_world(root / "content_analysis" / "world.yaml", vc)
    scen = {s["id"]: s for s in ca_syn.load_scenarios(root)}
    out: dict[str, Fixture] = {}
    for key in keys:
        s = scen[key]
        target = world.targets[s["target"]]
        analyzer, _ = ca_syn.analyzer_for(
            world, lc, vc, [ca_syn._video(v) for v in s.get("videos", ())], target.query
        )
        inp = ca_syn.inputs_for(world, s["target"], category=s.get("category"))
        res = asyncio.run(analyzer.analyse(inp))
        ev = None
        if res.bundle is not None:
            records = world.registry.store.get_many(res.bundle.evidence_ids)
            ev = EvidenceContext(
                world.registry,
                vc,
                res.bundle,
                tuple(records.values()),
                inp.catalog,
                inp.stats,
                world.now,
            )
        out[key] = Fixture(res.report, ev)
    return out


def load_split(root: Path, split: str) -> dict[str, Any]:
    path = root / "recommendations_eval" / f"eval_{split}.yaml"
    data: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))
    if data.get("split") != split:
        raise ValueError(f"{path}: split mismatch")
    return data


# ----------------------------------------------------------------- labelled drafts
def _draft(case: dict[str, Any]) -> ConceptDraft:
    scen = case["scenario"]
    return ConceptDraft.model_validate(
        {
            "video_format": case.get("video_format", _DEFAULT_FORMAT.get(scen)),
            "titles": [{"strategy": t["strategy"], "text": t["text"]} for t in case["titles"]],
            "hooks": [{"hook_type": h["hook_type"], "text": h["text"]} for h in case["hooks"]],
            "target_audience": case.get("audience", "viewers searching for this topic"),
            "core_viewer_problem": case.get("problem", "finding a direct answer to the search"),
            "premise": case.get("premise", "A video that answers the search query directly."),
            "why_it_may_satisfy_demand": case.get(
                "why", "It answers the question behind the query directly."
            ),
            "outline": [
                {
                    "heading": "Main",
                    "purpose": "the labelled statements",
                    "items": [
                        {
                            "label": i["label"],
                            "text": i["text"],
                            "evidence_ids": list(i.get("evidence", [])),
                        }
                        for i in case["items"]
                    ],
                },
                {
                    "heading": "Close",
                    "purpose": "summary",
                    "items": [
                        {"label": "PLANNED_DEMONSTRATION", "text": "Summarise what was shown"}
                    ],
                },
            ],
            "conclusion_to_investigate": "What the video shows.",
        }
    )


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, predicted: bool, actual: bool) -> None:
        if predicted and actual:
            self.tp += 1
        elif predicted:
            self.fp += 1
        elif actual:
            self.fn += 1
        else:
            self.tn += 1

    def as_dict(self) -> dict[str, Any]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        f1 = 2 * p * r / (p + r) if p and r else (0.0 if p == 0 or r == 0 else None)
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": round(p, 3) if p is not None else None,
            "recall": round(r, 3) if r is not None else None,
            "f1": round(f1, 3) if f1 is not None else None,
        }


def evaluate_drafts(
    cases: list[dict[str, Any]],
    fixtures: dict[str, Fixture],
    loaded: LoadedRecommendationConfig,
) -> dict[str, Any]:
    engine = RecommendationEngine(loaded)
    titles, hooks, removal = Confusion(), Confusion(), Confusion()
    bad_published: list[str] = []
    supported_total = supported_kept = 0
    status_ok = 0
    false_approvals: list[str] = []
    rows = []
    for case in cases:
        fx = fixtures[case["scenario"]]
        assert fx.evidence is not None, case["id"]
        ctx, problems = engine.context(fx.report, [], fx.evidence, CreatorProfile())
        assert ctx is not None, (case["id"], problems)
        v = verify_concept(_draft(case), ctx, fx.evidence, loaded, generator="eval:labelled")
        accepted_titles = {t.text for t in v.titles if t.accepted}
        for t in case["titles"]:
            titles.add(predicted=t["text"] not in accepted_titles, actual=t["gold"] == "reject")
        accepted_hooks = {h.text for h in v.hooks if h.accepted}
        for h in case["hooks"]:
            hooks.add(predicted=h["text"] not in accepted_hooks, actual=h["gold"] == "reject")
        final = {i.text: i.label for s in v.outline.sections for i in s.items}
        for item in case["items"]:
            text, gold = item["text"], item["gold"]
            kept_as_fact = final.get(text) is StatementLabel.VERIFIED_FACT
            present = text in final
            # "removed" = no longer presented as a fact (removed, or relabelled UNKNOWN)
            neutralised = not kept_as_fact if item["label"] == "VERIFIED_FACT" else not present
            if gold in _BAD:
                removal.add(predicted=neutralised, actual=True)
                if (gold == "unsafe" and present) or kept_as_fact:
                    bad_published.append(f"{case['id']}: {text}")
            elif gold == "supported":
                supported_total += 1
                supported_kept += kept_as_fact
                removal.add(predicted=not kept_as_fact, actual=False)
            else:  # plan
                removal.add(predicted=not present, actual=False)
        status = v.verification.status.value
        ok = status in case["acceptable_status"]
        status_ok += ok
        if status == "APPROVED" and "APPROVED" not in case["acceptable_status"]:
            false_approvals.append(case["id"])
        rows.append({"id": case["id"], "status": status, "acceptable": ok})
    n = len(cases)
    return {
        "cases": n,
        "status_accuracy": round(status_ok / n, 3) if n else None,
        "false_approvals": false_approvals,
        "unsupported_published_as_fact": bad_published,
        "unsupported_publication_rate": round(
            len(bad_published) / max(1, removal.tp + removal.fn), 3
        ),
        "bad_statement_removal": removal.as_dict(),
        "supported_fact_retention": round(supported_kept / supported_total, 3)
        if supported_total
        else None,
        "title_rejection": titles.as_dict(),
        "hook_rejection": hooks.as_dict(),
        "per_case": rows,
    }


def sweep_duplicates(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    out = []
    for t in [x / 20 for x in range(4, 19)]:  # 0.20 .. 0.90
        c = Confusion()
        for p in pairs:
            c.add(jaccard(normalized(p["a"]), normalized(p["b"])) >= t, bool(p["duplicate"]))
        out.append({"threshold": round(t, 2), **c.as_dict()})
    best = max(out, key=lambda r: (r["f1"] or 0, -abs(r["threshold"] - 0.6)))
    return {"sweep": out, "best_threshold": best["threshold"], "best_f1": best["f1"]}


def duplicates_at(pairs: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    c = Confusion()
    for p in pairs:
        c.add(jaccard(normalized(p["a"]), normalized(p["b"])) >= threshold, bool(p["duplicate"]))
    return {"threshold": threshold, **c.as_dict()}


# ------------------------------------------------------ generated recommendations
def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


_ATTRIBUTION = re.compile(r"^According to (?P<who>[^:]{2,80}):\s*(?P<claim>.+)$")


def grounded(text: str, cited: list[str], publishers: tuple[str, ...] = ()) -> bool:
    """Independent lexical check: >= 80% of the statement's content words appear in the
    text of the records it cites (excerpt and extracted facts). An "According to X:"
    attribution must name the publisher of a cited record; the claim after it is checked."""
    m = _ATTRIBUTION.match(text.strip())
    if m:
        who = m.group("who").strip().lower()
        if not any(who == (p or "").strip().lower() for p in publishers):
            return False
        text = m.group("claim")
    words = _tokens(text)
    if not words or not cited:
        return False
    source = set().union(*(_tokens(c) for c in cited))
    return len(words & source) / len(words) >= 0.8


def evaluate_generated(
    fixtures: dict[str, Fixture], loaded: LoadedRecommendationConfig
) -> dict[str, Any]:
    engine = RecommendationEngine(loaded)
    recs: list[RecommendationOutput] = []
    latencies: list[float] = []
    facts = facts_grounded = 0
    ungrounded: list[str] = []
    for fx in fixtures.values():
        if fx.evidence is None:
            continue
        t0 = time.perf_counter()
        oc = engine.recommend(fx.report, [], fx.evidence, CreatorProfile())
        latencies.append(time.perf_counter() - t0)
        texts = {
            r.evidence_id: " ".join(
                [
                    r.excerpt or "",
                    *(
                        " ".join(str(x) for x in (f.subject, f.attribute, f.value) if x)
                        for f in r.facts
                    ),
                ]
            )
            for r in fx.evidence.records
        }
        pubs = {r.evidence_id: r.publisher or "" for r in fx.evidence.records}
        for r in oc.recommendations:
            recs.append(r)
            for s in r.outline.sections:
                for i in s.items:
                    if i.label is StatementLabel.VERIFIED_FACT:
                        facts += 1
                        ok = grounded(
                            i.text,
                            [texts.get(e, "") for e in i.evidence_ids],
                            tuple(pubs.get(e, "") for e in i.evidence_ids),
                        )
                        facts_grounded += ok
                        if not ok:
                            ungrounded.append(i.text)
    approved = [r for r in recs if r.ranking.eligible]
    align = [
        intent_alignment(r.video_format, r.target_intent, r.target_sub_intent) for r in approved
    ]
    gap_fit = [
        r.gap_type is None
        or r.gap_type in ("insufficient_evidence", "no_verifiable_gap")
        or r.gap_type in _GAP_FIT.get(CATALOG[r.video_format].family, frozenset())
        for r in approved
    ]
    deep = loaded.config.feasibility.profiles
    max_hours = max(p.hours for p in deep.values()) * 1.5
    feasible = [r.production.estimated_hours_high <= max_hours for r in approved]
    sel = diversity.select(recs, loaded, limit=10)
    top = sel.ranked
    sims = [jaccard(normalized(a.title), normalized(b.title)) for a, b in combinations(top, 2)]
    families = {(CATALOG[r.video_format].family) for r in top}
    originality = [
        c.value
        for r in approved
        for c in r.ranking.components
        if c.name == "concept_originality" and c.value is not None
    ]
    return {
        "recommendations": len(recs),
        "eligible": len(approved),
        "research_candidates": sum(1 for r in recs if r.research_hypothesis),
        "search_intent_alignment": {
            "mean_format_alignment": round(statistics.fmean(align), 3) if align else None,
            "share_serving_intent": round(sum(a >= 0.7 for a in align) / len(align), 3)
            if align
            else None,
            "mean_title_query_coverage": round(
                statistics.fmean(
                    next(
                        (t.query_term_coverage for t in r.alternative_titles if t.text == r.title),
                        _title_coverage(r),
                    )
                    for r in approved
                ),
                3,
            )
            if approved
            else None,
        },
        "factual_grounding": {
            "verified_facts": facts,
            "lexically_grounded": facts_grounded,
            "unsupported_claim_rate": round(1 - facts_grounded / facts, 3) if facts else None,
            "ungrounded_examples": ungrounded[:5],
        },
        "content_gap_relevance": round(sum(gap_fit) / len(gap_fit), 3) if gap_fit else None,
        "originality_mean": round(statistics.fmean(originality), 3) if originality else None,
        "production_feasibility": {
            "share_within_profile_bounds": round(sum(feasible) / len(feasible), 3)
            if feasible
            else None,
            "max_hours_bound": max_hours,
        },
        "diversity_top10": {
            "items": len(top),
            "mean_pairwise_title_jaccard": round(statistics.fmean(sims), 3) if sims else None,
            "max_pairwise_title_jaccard": round(max(sims), 3) if sims else None,
            "distinct_format_families": len(families),
            "categories": sorted({r.category or "other" for r in top}),
            "suppressed": len(sel.suppressed),
        },
        "generation_seconds_per_opportunity": {
            "mean": round(statistics.fmean(latencies), 4) if latencies else None,
            "max": round(max(latencies), 4) if latencies else None,
        },
    }


def _title_coverage(r: RecommendationOutput) -> float:
    from trend_agent.recommendations.title_generator import query_coverage

    return query_coverage(r.primary_query, r.title)


# ------------------------------------------------------------------ entry point
@dataclass
class EvaluationReport:
    splits: dict[str, Any] = field(default_factory=dict)
    duplicates: dict[str, Any] = field(default_factory=dict)
    generated: dict[str, Any] = field(default_factory=dict)
    human_usefulness: dict[str, Any] = field(default_factory=dict)
    notice: str = (
        "Synthetic, author-labelled fixtures with fictional entities: these numbers describe "
        "the rules' behaviour on these cases only. They are not evidence of real-world "
        "recommendation accuracy or usefulness."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "splits": self.splits,
            "duplicates": self.duplicates,
            "generated": self.generated,
            "human_usefulness": self.human_usefulness,
            "notice": self.notice,
        }


def evaluate(
    root: Path,
    loaded: LoadedRecommendationConfig,
    *,
    splits: tuple[str, ...] = SPLITS,
    human_labels: Path | None = None,
) -> EvaluationReport:
    fixtures = load_fixtures(root)
    rep = EvaluationReport()
    data = {s: load_split(root, s) for s in splits}
    for s, d in data.items():
        rep.splits[s] = evaluate_drafts(d["drafts"], fixtures, loaded)
    configured = loaded.config.diversity.title_similarity_merge
    if "calibration" in data:
        sweep = sweep_duplicates(data["calibration"]["duplicates"])
        rep.duplicates["calibration"] = {
            "configured": duplicates_at(data["calibration"]["duplicates"], configured),
            "best_threshold": sweep["best_threshold"],
            "best_f1": sweep["best_f1"],
            "sweep": sweep["sweep"],
        }
    if "heldout" in data:
        rep.duplicates["heldout"] = {
            "configured": duplicates_at(data["heldout"]["duplicates"], configured)
        }
    rep.generated = evaluate_generated(fixtures, loaded)
    if human_labels is not None and human_labels.is_file():
        labels = yaml.safe_load(human_labels.read_text("utf-8")) or {}
        votes = [bool(v) for v in labels.values()]
        rep.human_usefulness = {
            "labelled": len(votes),
            "useful_rate": round(sum(votes) / len(votes), 3) if votes else None,
            "source": str(human_labels),
        }
    else:
        rep.human_usefulness = {
            "measured": False,
            "reason": "needs creator judgements: add a labels file (recommendation id -> "
            "true/false) or use recorded USEFUL / NOT_RELEVANT feedback in production",
        }
    return rep


__all__ = [
    "SPLITS",
    "EvaluationReport",
    "VideoFormat",
    "evaluate",
    "evaluate_drafts",
    "evaluate_generated",
    "grounded",
    "load_fixtures",
    "load_split",
    "sweep_duplicates",
]
