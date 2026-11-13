"""``trend-agent analyze ...`` commands (Phase 5: search intent and content-gap analysis).

Analyses compare what searchers appear to need with what the inspected YouTube results
appear to provide. Gaps are hypotheses about the inspected results; opportunity scores
rank research priorities and are not probabilities. Without a YouTube API key, analyses
use stored results only; without an enabled LLM they run deterministically.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.content_opportunity import OpportunityReport

analyze_app = typer.Typer(
    help="Search intent and content-gap analysis (Phase 5).",
    no_args_is_help=True,
)
NOTE = (
    "Content gaps are hypotheses about the inspected results; scores rank research "
    "priorities and are not probabilities. Only titles and descriptions are inspected "
    "unless authorised captions are available."
)
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Allow synthetic (demo/test) data.")
]
NoLLMOpt = Annotated[bool, typer.Option("--no-llm", help="Deterministic analysis only.")]
ForceOpt = Annotated[
    bool, typer.Option("--force", help="Re-analyse even if identical inputs were cached.")
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Do not persist the analysis.")]


def _service() -> tuple[Any, Any]:
    container = base._container(require_db=True)
    try:
        return container, container.content_analysis()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"content-analysis configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def _score(rep: OpportunityReport) -> str:
    return "n/a" if rep.score.score is None else f"{rep.score.score:.1f}"


def print_report(rep: OpportunityReport, *, verbose: bool = False) -> None:
    colour = {"candidate": "green", "no_opportunity": "yellow"}.get(rep.status.value, "red")
    syn = " [magenta](synthetic)[/]" if rep.synthetic else ""
    base.console.print(
        f"[bold]{escape(rep.canonical_query)}[/]{syn} -> [{colour}]{rep.status.value}[/] "
        f"score {_score(rep)} | verification {rep.verification_state}"
    )
    intent = rep.intent
    sub = f"/{intent.sub_intent.value}" if intent.sub_intent else ""
    base.console.print(
        f"intent: {intent.primary_intent.value}{sub} ({intent.confidence.value} confidence, "
        f"{intent.specificity.value}); entity: {escape(rep.entity or '-')}"
    )
    base.console.print(f"demand: {rep.demand.basis.value} - {escape(rep.demand.statement)}")
    base.console.print(
        f"inspected {rep.inspected_results} result(s) [{rep.region or '-'}/"
        f"{rep.language or '-'}]; evidence quality "
        f"{rep.evidence_quality.value if rep.evidence_quality else 'n/a'}"
    )
    if rep.primary_gap is not None:
        g = rep.primary_gap
        base.console.print(
            f"primary gap: [bold]{g.gap_type.value}[/] ({g.evidence_strength.value}, "
            f"{g.verification_status.value}) - {escape(g.statement)}"
        )
    for key, text in rep.explanation.items():
        base.console.print(f"  [cyan]{key.replace('_', ' ')}[/]: {escape(text)}")
    if rep.suggested_content_angle:
        base.console.print(f"  [cyan]angle[/]: {escape(rep.suggested_content_angle)}")
    base.console.print(f"main uncertainty: {escape(rep.main_uncertainty)}")
    if verbose:
        table = Table("component", "value", "weight", "note")
        for c in rep.score.components:
            table.add_row(
                c.name,
                "n/a" if c.value is None else f"{c.value:.2f}",
                f"{c.weight:.2f}",
                escape(c.note[:80]),
            )
        base.console.print(table)
        if rep.score.cap_reason:
            base.console.print(f"[dim]cap {rep.score.cap}: {escape(rep.score.cap_reason)}[/]")
        claims = Table("kind", "status", "pub", "claim", "why")
        for cl in rep.claims:
            claims.add_row(
                cl.kind.value,
                cl.status.value,
                "yes" if cl.published else "no",
                escape(cl.text[:90]),
                escape((cl.reasons or ("",))[0][:60]),
            )
        base.console.print(claims)
        for lim in rep.coverage_limitations:
            base.console.print(f"  [dim]limitation: {escape(lim)}[/]")
    base.console.print(f"[dim]{NOTE}[/]")


def _print_result(res: Any, as_json: bool, verbose: bool) -> None:
    if as_json:
        base._emit_json(
            {
                "status": res.status,
                "run_id": res.run_id,
                "created": res.created,
                "attempts": res.attempts,
                "error": res.error,
                "notes": res.notes,
                "report": res.report.model_dump(mode="json") if res.report else None,
            }
        )
        return
    if res.status == "failed":
        raise base._fail(f"analysis failed after {res.attempts} attempt(s): {res.error}", 1)
    base.console.print(
        f"[dim]{res.status} (run {res.run_id[:8] if res.run_id else '-'})"
        + (f"; {'; '.join(res.notes)}" if res.notes else "")
        + "[/]"
    )
    if res.report is not None:
        print_report(res.report, verbose=verbose)


@analyze_app.command("query")
def analyze_query(
    query: Annotated[str, typer.Option("--query", "-q", help="Search query to analyse.")],
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="ai | technology | gaming")
    ] = None,
    region: Annotated[str | None, typer.Option("--region")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    no_llm: NoLLMOpt = False,
    force: ForceOpt = False,
    dry_run: DryRunOpt = False,
    include_synthetic: SyntheticOpt = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Analyse one query on request (labelled as a manual analysis)."""
    container, svc = _service()

    async def go() -> Any:
        return await svc.analyse_query(
            query,
            category=category,
            region=region,
            language=language,
            include_synthetic=include_synthetic,
            use_llm=not no_llm,
            force=force,
            persist=not dry_run,
        )

    try:
        res = base._run(container, go)
    except (LookupError, PermissionError, ValueError) as exc:
        raise base._fail(str(exc), 2) from None
    _print_result(res, as_json, verbose)


@analyze_app.command("surge")
def analyze_surge(
    event_id: Annotated[int, typer.Option("--event-id", "-e", help="Phase 4 surge event id.")],
    no_llm: NoLLMOpt = False,
    force: ForceOpt = False,
    dry_run: DryRunOpt = False,
    include_synthetic: SyntheticOpt = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Analyse the query behind a Phase 4 surge event (its demand evidence is preserved)."""
    container, svc = _service()

    async def go() -> Any:
        return await svc.analyse_event(
            event_id,
            include_synthetic=include_synthetic,
            use_llm=not no_llm,
            force=force,
            persist=not dry_run,
        )

    try:
        res = base._run(container, go)
    except (LookupError, PermissionError, ValueError) as exc:
        raise base._fail(str(exc), 2) from None
    _print_result(res, as_json, verbose)


def _plan_rows(plan: Any) -> list[dict[str, Any]]:
    rows = [
        {
            "key": p.item.key,
            "query": p.item.query,
            "category": p.item.category,
            "basis": p.item.basis.value,
            "priority": p.priority,
            "urgency": p.urgency,
            "llm": p.use_llm,
            "decision": "selected",
        }
        for p in plan.selected
    ]
    rows += [
        {
            "key": it.key,
            "query": it.query,
            "category": it.category,
            "basis": it.basis.value,
            "priority": None,
            "urgency": None,
            "llm": False,
            "decision": f"skipped: {why}",
        }
        for it, why in plan.skipped
    ]
    return rows


@analyze_app.command("pending")
def analyze_pending(
    limit: Annotated[int | None, typer.Option("--limit", "-n", min=1, max=500)] = None,
    include_synthetic: SyntheticOpt = False,
    force: ForceOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Show what the next queue run would analyse, and why the rest is skipped."""
    container, svc = _service()
    try:
        plan = svc.pending(include_synthetic=include_synthetic, limit=limit, force=force)
    finally:
        _close(container)
    rows = _plan_rows(plan)
    if as_json:
        base._emit_json(
            {
                "search_budget_left": plan.search_budget_left,
                "llm_tokens_left": plan.llm_tokens_left,
                "items": rows,
            }
        )
        return
    table = Table("key", "query", "cat", "demand basis", "priority", "urgency", "llm", "decision")
    for r in rows:
        table.add_row(
            r["key"],
            escape(r["query"][:40]),
            r["category"] or "-",
            r["basis"],
            "-" if r["priority"] is None else f"{r['priority']:.1f}",
            r["urgency"] or "",
            "yes" if r["llm"] else "no",
            escape(r["decision"][:50]),
        )
    base.console.print(table)
    base.console.print(
        f"search requests left today: {plan.search_budget_left}; "
        f"LLM tokens left today: {plan.llm_tokens_left}"
    )


@analyze_app.command("run")
def analyze_run(
    limit: Annotated[int | None, typer.Option("--limit", "-n", min=1, max=500)] = None,
    include_synthetic: SyntheticOpt = False,
    force: ForceOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Run the cost-aware queue once (open surge events, bounded by budgets)."""
    container, svc = _service()

    async def go() -> Any:
        return await svc.run(include_synthetic=include_synthetic, limit=limit, force=force)

    report = base._run(container, go)
    if as_json:
        base._emit_json(
            {
                "counts": report.counts,
                "plan": _plan_rows(report.plan),
                "results": [
                    {
                        "key": p.item.key,
                        "status": r.status,
                        "run_id": r.run_id,
                        "error": r.error,
                        "opportunity_status": r.report.status.value if r.report else None,
                        "score": r.report.score.score if r.report else None,
                    }
                    for p, r in report.results
                ],
            }
        )
        return
    table = Table("key", "query", "result", "status", "score", "note")
    for p, r in report.results:
        table.add_row(
            p.item.key,
            escape(p.item.query[:40]),
            r.status,
            r.report.status.value if r.report else "-",
            _score(r.report) if r.report else "-",
            escape((r.error or "; ".join(r.notes))[:60]),
        )
    base.console.print(table)
    base.console.print(
        f"counts: {report.counts or {}}; skipped {len(report.plan.skipped)} "
        "(see `trend-agent analyze pending`)"
    )


@analyze_app.command("results")
def analyze_results(
    run_id: Annotated[
        str | None, typer.Option("--run", help="Analysis run id (default: latest).")
    ] = None,
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Retrieved results of one analysis with relevance, freshness and evidence level."""
    container, svc = _service()
    try:
        if run_id is None:
            runs = svc.runs(limit=1, include_synthetic=include_synthetic, query=query)
            if not runs:
                raise base._fail("no analysis runs found", 1)
            run_id = str(runs[0].id)
        data = svc.run_details(run_id)
    finally:
        _close(container)
    if data is None:
        raise base._fail(f"analysis run {run_id} not found", 1)
    if as_json:
        base._emit_json(data)
        return
    run = data["run"]
    base.console.print(
        f"[bold]{escape(run['query'])}[/] run {run['id'][:8]} {run['status']} "
        f"({run['results_inspected']} inspected, demand {run['demand_basis']})"
    )
    table = Table("pos", "video", "score", "rel", "entity", "format", "freshness", "lvl", "why")
    for v in data["results"]:
        table.add_row(
            str(v["api_position"]),
            v["video_id"],
            f"{v['relevance_score']:.1f}",
            "yes" if v["relevant"] else "no",
            v["entity_match"],
            v["format"],
            v["freshness"],
            v["evidence_level"][:1],
            escape((v["explanation"] or "")[:50]),
        )
    base.console.print(table)
    base.console.print(
        "[dim]API position is the order the API returned, not YouTube's ranking; relevance "
        "is a lexical estimate, not a viewer-satisfaction probability.[/]"
    )


@analyze_app.command("intent")
def analyze_intent(
    query: Annotated[str, typer.Option("--query", "-q")],
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Rules only; no database entity catalog.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Classify a query's intent and list its information requirements (no retrieval)."""
    from trend_agent.content_analysis.config import load_content_config
    from trend_agent.content_analysis.entity_verification import QueryEntities
    from trend_agent.content_analysis.intent_classifier import classify
    from trend_agent.content_analysis.query_decomposition import decompose

    if offline:
        loaded = load_content_config(resolve_config_dir(None))
        ents = QueryEntities()
        intent = classify(query, ents, loaded.config, category=category)
        dec = decompose(intent, ents)
    else:
        container, svc = _service()
        try:
            inp = svc.inputs_for_query(
                query, category=category, include_synthetic=include_synthetic
            )
            _, intent, dec = svc._analyzer(False).understand(inp)
        finally:
            _close(container)
    if as_json:
        base._emit_json(
            {"intent": intent.model_dump(mode="json"), "requirements": dec.model_dump(mode="json")}
        )
        return
    sub = f"/{intent.sub_intent.value}" if intent.sub_intent else ""
    base.console.print(
        f"[bold]{intent.primary_intent.value}{sub}[/] ({intent.confidence.value} confidence, "
        f"{intent.specificity.value}, method {intent.method.value})"
    )
    if intent.secondary_intents:
        base.console.print("secondary: " + ", ".join(i.value for i in intent.secondary_intents))
    for e in intent.entities:
        base.console.print(f"entity: {escape(e.name)} [{e.state}] {e.entity_type or ''}")
    for a in intent.ambiguity:
        base.console.print(f"[yellow]ambiguity:[/] {escape(a)}")
    for f in intent.policy_flags:
        base.console.print(f"[red]policy:[/] {escape(f)}")
    table = Table("key", "priority", "requirement", "blocking")
    for r in dec.requirements:
        table.add_row(r.key, r.priority.value, escape(r.requirement), "yes" if r.blocking else "")
    base.console.print(table)
    base.console.print(
        "[dim]Confidence labels are rule-based categories, not probabilities. Requirements "
        "are candidates; product details must be verified before they are relied on.[/]"
    )


@analyze_app.command("gaps")
def analyze_gaps(
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 30,
    include_non_gaps: Annotated[
        bool, typer.Option("--all", help="Include no_verifiable_gap / insufficient_evidence.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Detected content gaps (hypotheses) across stored analyses."""
    container, svc = _service()
    try:
        rows = svc.gaps(
            category=category,
            limit=limit,
            include_synthetic=include_synthetic,
            include_non_gaps=include_non_gaps,
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table("query", "gap", "strength", "verification", "explanation")
    for r in rows:
        table.add_row(
            escape(r["query"][:36]),
            r["gap_type"],
            r["strength"],
            r["verification"],
            escape(r["explanation"][:80]),
        )
    base.console.print(table)
    base.console.print(f"[dim]{NOTE}[/]")


@analyze_app.command("opportunities")
def analyze_opportunities(
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 20,
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include no_opportunity / insufficient / blocked.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Ranked content opportunities (research priorities, not predictions)."""
    statuses = (
        ("candidate", "no_opportunity", "insufficient_evidence", "blocked_policy")
        if all_statuses
        else ("candidate",)
    )
    container, svc = _service()
    try:
        reps = svc.opportunities(
            category=category, limit=limit, statuses=statuses, include_synthetic=include_synthetic
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json([r.model_dump(mode="json") for r in reps])
        return
    table = Table("id", "query", "cat", "score", "status", "gap", "demand basis", "verification")
    for r in reps:
        table.add_row(
            r.opportunity_id[:8],
            escape(r.canonical_query[:36]),
            r.category or "-",
            _score(r),
            r.status.value,
            r.primary_gap.gap_type.value if r.primary_gap else "-",
            r.demand.basis.value,
            r.verification_state,
        )
    base.console.print(table)
    base.console.print(f"[dim]{NOTE}[/]")


def _lookup(svc: Any, ident: str) -> OpportunityReport:
    found = svc.opportunity(ident)
    if found is None and len(ident) < 36:
        matches = [
            r
            for r in svc.opportunities(
                limit=500,
                statuses=("candidate", "no_opportunity", "insufficient_evidence", "blocked_policy"),
                include_synthetic=True,
            )
            if r.opportunity_id.startswith(ident)
        ]
        if len(matches) == 1:
            first: OpportunityReport = matches[0]
            return first
        if len(matches) > 1:
            raise base._fail(f"opportunity id prefix {ident!r} is ambiguous", 2)
    if found is None:
        raise base._fail(f"opportunity {ident} not found", 1)
    rep: OpportunityReport = found[0]
    return rep


@analyze_app.command("explain")
def analyze_explain(
    opportunity_id: Annotated[str, typer.Argument(help="Opportunity id (or unique prefix).")],
    as_json: base.JsonOpt = False,
) -> None:
    """Full opportunity report: demand, coverage, gaps, score components and claims."""
    container, svc = _service()
    try:
        rep = _lookup(svc, opportunity_id)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rep.model_dump(mode="json"))
        return
    print_report(rep, verbose=True)
    table = Table("requirement", "status", "evidence")
    for s in rep.requirement_coverage:
        table.add_row(escape(s.requirement[:50]), s.best_status.value, escape(s.statement[:70]))
    base.console.print(table)


@analyze_app.command("verify")
def analyze_verify(
    opportunity_id: Annotated[str, typer.Argument(help="Opportunity id (or unique prefix).")],
    as_json: base.JsonOpt = False,
) -> None:
    """Re-check a stored report's claims against the evidence registry now (read-only)."""
    container, svc = _service()
    try:
        rep = _lookup(svc, opportunity_id)
        out = svc.reverify(rep.opportunity_id)
    finally:
        _close(container)
    assert out is not None
    _, checks = out
    stale = [c for c in checks if c.was_published and not c.still_valid]
    if as_json:
        base._emit_json(
            {
                "opportunity_id": rep.opportunity_id,
                "published_claims_still_valid": not stale,
                "checks": [c.__dict__ for c in checks],
            }
        )
        return
    table = Table("kind", "published", "still valid", "claim", "problem")
    for c in checks:
        table.add_row(
            c.kind,
            "yes" if c.was_published else "no",
            "[green]yes[/]" if c.still_valid else "[red]no[/]",
            escape(c.text[:70]),
            escape((c.problems or ("",))[0][:60]),
        )
    base.console.print(table)
    if stale:
        base.console.print(
            f"[red]{len(stale)} published claim(s) no longer resolve[/]; re-run the analysis."
        )
    else:
        base.console.print("[green]all published claims still resolve to valid evidence[/]")


@analyze_app.command("status")
def analyze_status(as_json: base.JsonOpt = False) -> None:
    """Runs, today's search/LLM usage against budgets, and capability mode."""
    container, svc = _service()
    try:
        data = svc.status()
    finally:
        _close(container)
    if as_json:
        base._emit_json(data)
        return
    base.console.print(f"retrieval: {escape(data['retrieval'])}")
    base.console.print(f"LLM: {escape(data['llm'])}")
    base.console.print(
        f"versions: {data['analysis_version']} / {escape(data['configuration_version'])}"
    )
    for key in ("runs_by_status", "runs_by_demand_basis", "today", "budgets"):
        base.console.print(
            f"[bold]{key}[/]: " + ", ".join(f"{k}={v}" for k, v in data[key].items())
        )
    base.console.print(
        "[bold]expired rows awaiting purge[/]: "
        + ", ".join(f"{k}={v}" for k, v in data["expired_rows_awaiting_purge"].items())
    )
    for m in data["disabled_metrics"]:
        base.console.print(f"  [dim]disabled: {escape(m)}[/]")


@analyze_app.command("demo")
def analyze_demo(
    scenario: Annotated[
        list[str] | None, typer.Option("--scenario", "-s", help="Scenario id(s), e.g. A B")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Run the synthetic scenarios offline (fictional entities; no database or network)."""
    from trend_agent.content_analysis.config import load_content_config
    from trend_agent.content_analysis.synthetic import load_scenarios, run_scenario
    from trend_agent.verification.config import load_verification_config

    root = resolve_config_dir(None)
    loaded, vloaded = load_content_config(root), load_verification_config(root)
    wanted = {s.upper() for s in scenario or ()}
    results = [
        run_scenario(s, loaded, vloaded, root)
        for s in load_scenarios(root)
        if not wanted or s["id"].upper() in wanted
    ]
    if as_json:
        base._emit_json(
            [
                {
                    "scenario": r.scenario,
                    "name": r.name,
                    "passed": r.passed,
                    "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in r.checks],
                    "report": r.outcome.report.model_dump(mode="json"),
                }
                for r in results
            ]
        )
        return
    for r in results:
        mark = "[green]PASS[/]" if r.passed else "[red]FAIL[/]"
        base.console.rule(f"{r.scenario} - {escape(r.name)} {mark}")
        print_report(r.outcome.report, verbose=verbose)
        for name, ok, detail in r.checks:
            if not ok:
                base.console.print(f"  [red]check failed[/] {name}: {escape(detail)}")
    passed = sum(r.passed for r in results)
    base.console.print(
        f"\n{passed}/{len(results)} scenarios met their expectations. Synthetic, fictional "
        "data: this demonstrates behaviour, not real-world accuracy."
    )
    if passed != len(results):
        raise typer.Exit(1)
