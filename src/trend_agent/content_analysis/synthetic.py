"""Synthetic scenarios A-J (fictional world) for the demo, tests and regression checks.

The world (entities, official records, Phase 4 statistics) is built with the verification
layer's fixture builder; each scenario adds the retrieved videos a fixture source returns
and the behaviour to check. Nothing here touches the network or a production database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from trend_agent.content_analysis.config import LoadedContentConfig
from trend_agent.content_analysis.demand import from_statistics
from trend_agent.content_analysis.llm_client import AnalysisLLM, ScriptedAnalysisLLM
from trend_agent.content_analysis.orchestrator import (
    AnalysisInputs,
    AnalysisOutcome,
    ContentAnalyzer,
    MemoryRegistrar,
)
from trend_agent.content_analysis.result_retrieval import FixtureResultSource
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.world import World, load_world


def _video(v: dict[str, Any], *, transcript: bool = False) -> dict[str, Any]:
    out = {
        "video_id": str(v["id"]),
        "title": v["t"],
        "description": v.get("d") or None,
        "channel_id": f"UC_{v.get('ch', 'unknown').replace(' ', '')}",
        "channel_title": v.get("ch"),
        "published_at": f"{v['p']}T12:00:00+00:00" if v.get("p") else None,
        "duration_seconds": v.get("s"),
        "default_language": v.get("lang"),
    }
    if transcript and v.get("transcript"):
        out["transcript"] = v["transcript"]
        out["transcript_authorised"] = True
    return out


@dataclass
class ScenarioResult:
    scenario: str
    name: str
    outcome: AnalysisOutcome
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def load_scenarios(root: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load((root / "content_analysis" / "scenarios.yaml").read_text("utf-8"))
    scenarios = list(data["scenarios"])
    by_id = {s["id"]: s for s in scenarios}
    for s in scenarios:
        if "same_videos_as" in s:
            s["videos"] = by_id[s["same_videos_as"]]["videos"]
    return scenarios


def analyzer_for(
    world: World,
    loaded: LoadedContentConfig,
    vloaded: LoadedVerificationConfig,
    videos: Sequence[dict[str, Any]],
    query: str,
    *,
    llm: AnalysisLLM | None = None,
    variant_results: dict[str, Sequence[dict[str, Any]]] | None = None,
) -> tuple[ContentAnalyzer, FixtureResultSource]:
    results: dict[str, Sequence[dict[str, Any]]] = {query: list(videos)}
    results.update(variant_results or {})
    source = FixtureResultSource(results, collected_at=world.now - timedelta(hours=1))
    config = loaded
    if llm is not None:
        config = loaded.model_copy(
            update={"config": loaded.config.model_copy(
                update={"llm": loaded.config.llm.model_copy(update={"enabled": True})})}
        )  # fmt: skip
    analyzer = ContentAnalyzer(
        config,
        MemoryRegistrar(world.registry),
        verification_config=vloaded.config,
        source=source,
        llm=llm,
        clock=lambda: world.now,
    )
    return analyzer, source


def inputs_for(world: World, target: str, *, category: str | None = None) -> AnalysisInputs:
    t = world.targets[target]
    records = [world.records[k] for k in t.record_keys]
    stats = world.statistics(t)
    surge = world.surge_records.get(target)
    if surge is not None and stats.available:
        records.append(surge)
    demand = from_statistics(
        stats, query=t.query, query_id=t.query_id, category=t.category, topic_cluster_id=t.topic_id
    )
    return AnalysisInputs(
        query=t.query,
        query_id=t.query_id,
        category=category or t.category,
        region="US",
        language="en",
        demand=demand,
        stats=stats,
        context_records=records,
        catalog=world.catalog,
        now=world.now,
        topic_cluster_id=t.topic_id,
        target_id=t.target_id,
        synthetic=True,
    )


def run_scenario(
    scenario: dict[str, Any],
    loaded: LoadedContentConfig,
    vloaded: LoadedVerificationConfig,
    root: Path,
) -> ScenarioResult:
    world = load_world(root / "content_analysis" / "world.yaml", vloaded)
    target = scenario["target"]
    query = world.targets[target].query
    videos = [_video(v) for v in scenario.get("videos", ())]
    llm = ScriptedAnalysisLLM(scenario["llm"]) if scenario.get("llm") else None
    analyzer, _ = analyzer_for(world, loaded, vloaded, videos, query, llm=llm)
    inp = inputs_for(world, target, category=scenario.get("category"))
    outcome = asyncio.run(analyzer.analyse(inp))
    result = ScenarioResult(scenario["id"], scenario.get("name", ""), outcome)
    result.checks = check(scenario.get("expect", {}), outcome)
    if "llm_does_not_change" in scenario.get("expect", {}):
        plain, _ = analyzer_for(world, loaded, vloaded, videos, query)
        base = asyncio.run(plain.analyse(inputs_for(world, target)))
        for key in scenario["expect"]["llm_does_not_change"]:
            a = getattr(outcome.report, key)
            b = getattr(base.report, key)
            a = a.score if key == "score" else a
            b = b.score if key == "score" else b
            result.checks.append((f"llm does not change {key}", a == b, f"{a} vs {b}"))
    return result


def _text(outcome: AnalysisOutcome) -> str:
    r = outcome.report
    parts = [*r.explanation.values(), r.suggested_content_angle or "", r.demand.statement]
    parts += [c.text for c in r.claims if c.published]
    parts += [g.statement for g in r.gaps]
    return "\n".join(parts)


def check(expect: dict[str, Any], outcome: AnalysisOutcome) -> list[tuple[str, bool, str]]:
    r = outcome.report
    out: list[tuple[str, bool, str]] = []
    rel = {a.video_id: a for a in outcome.relevance}
    fresh = {f.video_id: f for f in outcome.freshness}
    gap_types = {g.gap_type.value for g in r.gaps}

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, ok, detail))

    if "primary_intent" in expect:
        add(
            "primary intent",
            r.intent.primary_intent.value == expect["primary_intent"],
            r.intent.primary_intent.value,
        )
    if "sub_intent" in expect:
        sub = r.intent.sub_intent.value if r.intent.sub_intent else None
        add("sub-intent", sub == expect["sub_intent"], str(sub))
    if "entity" in expect:
        add("entity", r.entity == expect["entity"], str(r.entity))
    if "entity_key" in expect:
        keys = [e.entity_key for e in r.intent.entities]
        add("entity key", expect["entity_key"] in keys, str(keys))
    if "status" in expect:
        add("status", r.status.value == expect["status"], r.status.value)
    if "not_status" in expect:
        add("status is not", r.status.value != expect["not_status"], r.status.value)
    for g in expect.get("gap_types", ()):
        add(f"gap {g}", g in gap_types, ", ".join(sorted(gap_types)))
    for g in expect.get("not_gap_types", ()):
        add(f"no gap {g}", g not in gap_types, ", ".join(sorted(gap_types)))
    for v in expect.get("relevant_includes", ()):
        add(
            f"{v} relevant",
            bool(rel.get(v) and rel[v].relevant),
            rel[v].explanation if v in rel else "missing",
        )
    for v in expect.get("relevant_excludes", ()):
        add(
            f"{v} not relevant",
            bool(rel.get(v)) and not rel[v].relevant,
            rel[v].explanation if v in rel else "missing",
        )
    for v in expect.get("version_mismatch", ()):
        add(
            f"{v} version mismatch",
            bool(rel.get(v)) and rel[v].entity_match.value == "version_mismatch",
            rel[v].entity_match.value if v in rel else "missing",
        )
    for v, status in expect.get("freshness", {}).items():
        add(
            f"{v} freshness {status}",
            bool(fresh.get(v)) and fresh[v].status.value == status,
            fresh[v].status.value if v in fresh else "missing",
        )
    for status in expect.get("not_freshness", ()):
        add(f"no {status}", all(f.status.value != status for f in outcome.freshness), "")
    if expect.get("ambiguity"):
        add("ambiguity preserved", bool(r.intent.ambiguity), "; ".join(r.intent.ambiguity))
    if "gap_verification" in expect:
        g = r.primary_gap
        got = g.verification_status.value if g is not None else "none"
        add("gap verification", got == expect["gap_verification"], got)
    if expect.get("coverage_not"):
        add(
            "coverage labels",
            all(
                c.status.value
                in ("covered", "partially_covered", "not_evidenced", "contradicted", "unknown")
                for c in outcome.cells
            ),
            "",
        )
    if expect.get("narrative_rejected"):
        narrative = [c for c in r.claims if c.kind.value == "narrative"]
        add(
            "LLM narrative checked",
            bool(narrative)
            and not any(
                c.published and ("fail" in c.text.lower() or "million" in c.text.lower())
                for c in narrative
            ),
            f"{sum(c.published for c in narrative)}/{len(narrative)} published",
        )
    if expect.get("no_location_claims"):
        add(
            "no location proposed",
            not any("location" in c.text.lower() and c.kind.value == "fact" for c in r.claims),
            "",
        )
        add("no score", r.score.score is None, str(r.score.score))
    for v in expect.get("injection_flagged", ()):
        vid = next((x for x in outcome.videos if x.video_id == v), None)
        add(
            f"{v} injection flagged",
            bool(vid and vid.injection_flags),
            ",".join(vid.injection_flags) if vid else "missing",
        )
    for s in expect.get("forbidden_output", ()):
        add(f"no '{s}' in output", s.lower() not in _text(outcome).lower(), "")
    if "demand_basis" in expect:
        add("demand basis", r.demand.basis.value == expect["demand_basis"], r.demand.basis.value)
    if "score_at_most" in expect:
        add(
            "score capped",
            r.score.score is None or r.score.score <= expect["score_at_most"],
            str(r.score.score),
        )
    if "trend_statement_contains" in expect:
        add(
            "trend statement",
            expect["trend_statement_contains"] in r.demand.statement,
            r.demand.statement,
        )
    # invariants for every scenario
    add(
        "every published claim verified",
        all(
            c.status.value in ("verified", "corroborated") or c.label
            for c in r.claims
            if c.published
        ),
        "",
    )
    add(
        "no score without evidence",
        not (r.status.value == "insufficient_evidence" and r.score.score is not None),
        str(r.score.score),
    )
    return out


def run_all(
    loaded: LoadedContentConfig, vloaded: LoadedVerificationConfig, root: Path
) -> list[ScenarioResult]:
    return [run_scenario(s, loaded, vloaded, root) for s in load_scenarios(root)]
