"""``trend-agent recommend ...`` and ``trend-agent dashboard ...`` (Phase 7).

Recommendations are research-backed video *ideas* for a creator to evaluate: nothing here
creates, uploads or publishes a video. Scores are research-priority indices from versioned
weights, not probabilities that a video will succeed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.dashboard import GenerateResult
from trend_agent.schemas.recommendation import RecommendationOutput

recommend_app = typer.Typer(
    help="AI video recommendations from verified opportunities (Phase 7).", no_args_is_help=True
)
dashboard_app = typer.Typer(help="Intelligence dashboard API (Phase 7).", no_args_is_help=True)
NOTE = (
    "Ideas to evaluate, not instructions or guarantees: scores rank research priority and "
    "are not probabilities of success. Nothing is uploaded or published."
)
CategoryOpt = Annotated[
    str | None, typer.Option("--category", help="ai | technology | gaming", show_default=False)
]
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Include synthetic (test) records.")
]
IdOpt = Annotated[str, typer.Option("--id", help="Recommendation ID (UUID).")]
_CATEGORIES = ("ai", "technology", "gaming")


def _check_category(category: str | None) -> None:
    if category is not None and category not in _CATEGORIES:
        raise base._fail(f"--category must be one of {', '.join(_CATEGORIES)}", 2)


def _service(include_synthetic: bool = False) -> tuple[Any, Any]:
    container = base._container(require_db=True)
    try:
        return container, container.recommendations(include_synthetic=include_synthetic)
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"recommendation configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def demo_cache_path() -> Path:
    settings = base.SETTINGS_FACTORY()
    if settings.demo_cache_path:
        return Path(settings.demo_cache_path)
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "trend-agent" / "demo_snapshot.json"


# ------------------------------------------------------------------ printers
def print_result(r: GenerateResult) -> None:
    syn = " [magenta](synthetic)[/]" if r.synthetic else ""
    dry = " [yellow](dry run: nothing stored)[/]" if r.dry_run else ""
    base.console.print(
        f"Run {r.run_id}{syn}{dry}: {r.considered} opportunities considered, "
        f"{r.generated} ideas generated, {r.reused} reused (unchanged evidence)"
    )
    base.console.print(
        f"  approved {r.approved} | research candidates {r.research} | rejected {r.rejected} "
        f"| suppressed (duplicates/diversity) {r.suppressed}"
    )
    cost = f"${r.estimated_cost_usd:.4f}" if r.estimated_cost_usd is not None else "unknown"
    base.console.print(
        f"  generator {r.generator}; LLM calls {r.llm_calls}, tokens "
        f"{r.input_tokens}+{r.output_tokens}, estimated cost {cost}; {r.seconds:.2f} s"
    )
    for s in r.skipped:
        base.console.print(f"  [dim]skipped: {escape(s)}[/]")
    for n in r.notes:
        base.console.print(f"  [yellow]note:[/] {escape(n)}")


def print_list(recs: list[RecommendationOutput]) -> None:
    t = Table(title="Video recommendations", show_lines=False)
    for col in ("rank", "score", "status", "title", "format", "hours", "urgency", "id"):
        t.add_column(col)
    for r in recs:
        p = r.production
        t.add_row(
            str(r.ranking.rank or "-"),
            f"{r.ranking.composite_score:.1f}" if r.ranking.composite_score is not None else "-",
            r.verification.status.value + (" [magenta]SYN[/]" if r.synthetic else ""),
            escape(r.title),
            r.format_label,
            f"{p.estimated_hours_low:g}-{p.estimated_hours_high:g}",
            p.urgency.value,
            r.recommendation_id,
        )
    base.console.print(t)
    base.console.print(f"[dim]{NOTE}[/]")


def print_explain(r: RecommendationOutput) -> None:
    syn = " [magenta][SYNTHETIC][/]" if r.synthetic else ""
    base.console.print(f"[bold]{escape(r.title)}[/]{syn}")
    base.console.print(
        f"query: {escape(r.primary_query)} | format: {r.format_label} | status: "
        f"{r.verification.status.value} | generator: {r.generator}"
    )
    rk = r.ranking
    score = f"{rk.composite_score:.1f}" if rk.composite_score is not None else "not ranked"
    base.console.print(
        f"score {score} (ranking {rk.version}; available weight {rk.available_weight:.0%}; "
        f"evidence-quality factor {rk.evidence_quality_factor:.2f})"
    )
    t = Table(title="Score components (missing = excluded, never zero)")
    for col in ("component", "value", "weight", "contribution", "note"):
        t.add_column(col)
    for c in rk.components:
        t.add_row(
            c.name,
            f"{c.value:.3f}" if c.value is not None else "missing",
            f"{c.weight:.2f}",
            f"{c.contribution:.1f}" if c.contribution is not None else "-",
            escape(c.note),
        )
    base.console.print(t)
    base.console.print(escape(rk.explanation))
    base.console.print("[bold]Verification[/]")
    for v in r.verification.checks:
        mark = "[green]pass[/]" if v.passed else "[red]fail[/]"
        base.console.print(f"  {mark} {v.check} {escape(v.detail)}")
    for x in r.verification.removed:
        base.console.print(f"  [yellow]removed:[/] {escape(x)}")
    for x in r.uncertainties:
        base.console.print(f"  [dim]uncertainty: {escape(x)}[/]")
    base.console.print("[bold]Outline[/]")
    for sec in r.outline.sections:
        base.console.print(f"  {escape(sec.heading)}")
        for it in sec.items:
            base.console.print(f"    [{it.label.value}] {escape(it.text)}")
    base.console.print(f"[dim]{escape(r.notice)}[/]")


# ------------------------------------------------------------------ commands
@recommend_app.command("generate")
def generate(
    category: CategoryOpt = None,
    all_categories: Annotated[bool, typer.Option("--all", help="Every category.")] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, max=500, help="Max opportunities.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Template generator only; store nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Regenerate even when the evidence is unchanged.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    json_out: base.JsonOpt = False,
) -> None:
    """Generate recommendations for the top verified opportunities."""
    _check_category(category)
    if category is None and not all_categories:
        raise base._fail("pass --category ai|technology|gaming or --all", 2)
    container, svc = _service(include_synthetic)
    try:
        res = svc.generate(
            category=category,
            all_categories=all_categories,
            max_opportunities=limit,
            dry_run=dry_run,
            force=force,
            trigger="cli",
        )
    finally:
        _close(container)
    if json_out:
        base._emit_json(res.model_dump(mode="json"))
        return
    print_result(res)


@recommend_app.command("list")
def list_cmd(
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    category: CategoryOpt = None,
    status: Annotated[str | None, typer.Option("--status", show_default=False)] = None,
    include_research: Annotated[
        bool, typer.Option("--include-research", help="Also show research candidates.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    json_out: base.JsonOpt = False,
) -> None:
    """List current recommendations in rank order."""
    _check_category(category)
    container, svc = _service(include_synthetic)
    try:
        recs = svc.list(
            limit=limit,
            category=category,
            status=status,
            include_research=include_research,
            sort="rank",
        )
    finally:
        _close(container)
    if json_out:
        base._emit_json([r.model_dump(mode="json") for r in recs])
        return
    if not recs:
        base.console.print(
            "No current recommendations. Run `trend-agent recommend generate --all` after "
            "content analysis has produced candidate opportunities."
        )
        return
    print_list(recs)


@recommend_app.command("explain")
def explain(rid: IdOpt, json_out: base.JsonOpt = False) -> None:
    """Score components, verification checks, labels and outline of one recommendation."""
    container, svc = _service(include_synthetic=True)
    try:
        rec = svc.get(rid)
    finally:
        _close(container)
    if rec is None:
        raise base._fail(f"recommendation {rid} not found", 4)
    if json_out:
        base._emit_json(rec.model_dump(mode="json"))
        return
    print_explain(rec)


@recommend_app.command("verify")
def verify(rid: IdOpt, json_out: base.JsonOpt = False) -> None:
    """Re-check a stored recommendation against the evidence registry now (read-only)."""
    container, svc = _service(include_synthetic=True)
    try:
        rep = svc.verify(rid)
    finally:
        _close(container)
    if rep is None:
        raise base._fail(f"recommendation {rid} not found", 4)
    if json_out:
        base._emit_json(rep.__dict__)
        return
    colour = "green" if rep.still_valid else "red"
    base.console.print(
        f"{rid}: stored status {rep.status}; still valid: [{colour}]{rep.still_valid}[/]; "
        f"current: {rep.current}"
    )
    for p in rep.problems:
        base.console.print(f"  [red]problem:[/] {escape(p)}")
    for c in rep.checks:
        mark = "[green]pass[/]" if c["passed"] else "[red]fail[/]"
        base.console.print(f"  {mark} {c['check']} {escape(str(c.get('detail', '')))}")
    if not rep.still_valid:
        raise typer.Exit(1)


@recommend_app.command("stats")
def stats(include_synthetic: SyntheticOpt = False, json_out: base.JsonOpt = False) -> None:
    """Counts by status and category, feedback events, recent runs, LLM usage and budget."""
    container, svc = _service(include_synthetic)
    try:
        out = svc.stats()
    finally:
        _close(container)
    if json_out:
        base._emit_json(out)
        return
    base.console.print(f"by status: {out['by_status']}")
    base.console.print(
        f"ranked {out['ranked']} | research {out['research']} | suppressed {out['suppressed']}"
    )
    base.console.print(f"by category (ranked): {out['by_category']}")
    base.console.print(f"feedback events: {out['feedback'] or 'none recorded'}")
    llm = out["llm"]
    base.console.print(
        f"LLM: {llm['mode']}; tokens today {llm['tokens_today']}/{llm['daily_token_budget']}"
    )
    for r in out["runs"]:
        cost = f"${r['estimated_cost_usd']:.4f}" if r["estimated_cost_usd"] is not None else "-"
        base.console.print(
            f"  run {r['run_id'][:8]} {r['started_at']} {r['status']} {r['generator']}: "
            f"{r['considered']} opp, {r['generated']} new, {r['reused']} reused, "
            f"{r['llm_calls']} LLM calls, {cost}"
        )
    base.console.print(f"[dim]{out['notice']}[/]")


@recommend_app.command("demo")
def demo(
    category: CategoryOpt = None,
    rebuild: Annotated[bool, typer.Option("--rebuild", help="Ignore the cached snapshot.")] = False,
    json_out: base.JsonOpt = False,
) -> None:
    """Run the isolated synthetic demonstration (fictional AI, technology and gaming
    scenarios). Never reads or writes the database."""
    from trend_agent.recommendations.demo import demo_backend

    _check_category(category)
    cache = demo_cache_path()
    if rebuild and cache.exists():
        cache.unlink()
    if not cache.exists():
        base.err_console.print("Building the synthetic demo snapshot (about a minute)...")
    backend = demo_backend(resolve_config_dir(None), cache)
    page = backend.recommendations(category=category, sort="rank", page_size=50)
    research = backend.recommendations(category=category, include_research=True, page_size=50)
    recs = [backend.recs[s.id] for s in page.items]
    if json_out:
        base._emit_json(
            {
                "synthetic": True,
                "recommendations": [r.model_dump(mode="json") for r in recs],
                "research_candidates": [
                    s.model_dump(mode="json") for s in research.items if s.research
                ],
                "alerts": [a.model_dump(mode="json") for a in backend.alerts()],
            }
        )
        return
    base.console.print(
        "[bold magenta]SYNTHETIC DEMONSTRATION[/]: fictional products, games and data. "
        "Nothing here is real demand or a real recommendation."
    )
    print_list(recs)
    rc = [s for s in research.items if s.research]
    if rc:
        base.console.print("[bold]Research candidates (not ranked)[/]")
        for s in rc:
            base.console.print(f"  {escape(s.title)} [{s.status}] {s.id}")
    alerts = backend.alerts()
    if alerts:
        base.console.print(f"[bold]In-app alerts[/] ({len(alerts)}, synthetic, never sent)")
        for a in alerts[:10]:
            base.console.print(f"  {a.event_type}: {escape(a.title)}")


@dashboard_app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    demo_mode: Annotated[
        bool, typer.Option("--demo", help="Serve the isolated synthetic demo (no database).")
    ] = False,
    ui: Annotated[
        bool,
        typer.Option("--ui", help="Also start the built Next.js dashboard (frontend/)."),
    ] = False,
    ui_port: Annotated[int, typer.Option("--ui-port", min=1, max=65535)] = 3000,
) -> None:
    """Serve the dashboard API (FastAPI). The browser UI is the Next.js app in frontend/;
    it calls this API through its own server, so the API token never reaches the browser."""
    import uvicorn

    from trend_agent.api.app import create_app

    settings = base.SETTINGS_FACTORY()
    if demo_mode:
        settings = settings.model_copy(
            update={"demo_mode": True, "demo_cache_path": str(demo_cache_path())}
        )
    if not settings.demo_mode:
        container = base._container(require_db=True)
    else:
        container = base.CONTAINER_FACTORY(settings)
        if not demo_cache_path().exists():
            base.err_console.print("Building the synthetic demo snapshot (about a minute)...")
    try:
        backend = container.dashboard_backend()
    except ConfigError as exc:
        raise base._fail(f"configuration: {exc}", 2) from None
    app = create_app(backend, settings)
    if host not in ("127.0.0.1", "localhost", "::1") and settings.api_token is None:
        base.err_console.print(
            "[yellow]Warning:[/] no TREND_AGENT_API_TOKEN: the API is read-only for public "
            "endpoints and refuses private data and every write."
        )
    child = None
    if ui:
        frontend = Path(__file__).resolve().parents[2] / "frontend"
        if not (frontend / ".next").exists():
            raise base._fail("frontend/ is not built: run `npm ci && npm run build` there", 2)
        env = {
            **os.environ,
            "PORT": str(ui_port),
            "TREND_AGENT_API_URL": f"http://{host}:{port}",
        }
        child = subprocess.Popen(["npm", "run", "start"], cwd=frontend, env=env)  # noqa: S607
        base.console.print(f"Dashboard UI: http://localhost:{ui_port}")
    base.console.print(
        f"Dashboard API ({backend.mode}{', SYNTHETIC' if backend.synthetic else ''}) on "
        f"http://{host}:{port}/api"
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)
    finally:
        if child is not None:
            child.terminate()
        container.engine.dispose()


@recommend_app.command("evaluate")
def evaluate_cmd(
    split: Annotated[str, typer.Option("--split", help="calibration | heldout | all")] = "all",
    human_labels: Annotated[
        Path | None,
        typer.Option("--human-labels", help="YAML: recommendation id -> useful (true/false)."),
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write the JSON report here.")] = None,
    json_out: base.JsonOpt = False,
) -> None:
    """Offline evaluation on author-labelled synthetic fixtures (no database, no LLM).
    The results describe the rules' behaviour on these cases, not real-world accuracy."""
    import json as _json

    from trend_agent.recommendations.config import load_recommendation_config
    from trend_agent.recommendations.evaluation import SPLITS, evaluate

    if split not in (*SPLITS, "all"):
        raise base._fail("--split must be calibration, heldout or all", 2)
    root = resolve_config_dir(None)
    splits = SPLITS if split == "all" else (split,)
    rep = evaluate(
        root, load_recommendation_config(root), splits=splits, human_labels=human_labels
    ).as_dict()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(rep, indent=2, default=str) + "\n", "utf-8")
    if json_out:
        base._emit_json(rep)
        return
    for name, s in rep["splits"].items():
        base.console.print(
            f"[bold]{name}[/] ({s['cases']} labelled drafts): status accuracy "
            f"{s['status_accuracy']}, false approvals {len(s['false_approvals'])}, "
            f"unsupported statements published as fact {len(s['unsupported_published_as_fact'])}"
        )
        r = s["bad_statement_removal"]
        base.console.print(
            f"  bad-statement removal precision {r['precision']} recall {r['recall']}; "
            f"supported-fact retention {s['supported_fact_retention']}; title rejection F1 "
            f"{s['title_rejection']['f1']}; hook rejection F1 {s['hook_rejection']['f1']}"
        )
    d = rep["duplicates"]
    if "calibration" in d:
        base.console.print(
            f"duplicates (calibration): configured F1 {d['calibration']['configured']['f1']}, "
            f"best threshold {d['calibration']['best_threshold']}"
        )
    if "heldout" in d:
        base.console.print(f"duplicates (held-out): F1 {d['heldout']['configured']['f1']}")
    g = rep["generated"]
    base.console.print(
        f"generated: {g['recommendations']} ideas; intent alignment "
        f"{g['search_intent_alignment']['mean_format_alignment']}; unsupported-claim rate "
        f"{g['factual_grounding']['unsupported_claim_rate']}; gap relevance "
        f"{g['content_gap_relevance']}; top-10 max title similarity "
        f"{g['diversity_top10']['max_pairwise_title_jaccard']}"
    )
    hu = rep["human_usefulness"]
    base.console.print(
        f"human usefulness: {hu.get('useful_rate', 'not measured')}"
        + ("" if hu.get("labelled") else f" ({hu.get('reason', '')})")
    )
    base.console.print(f"[dim]{rep['notice']}[/]")
