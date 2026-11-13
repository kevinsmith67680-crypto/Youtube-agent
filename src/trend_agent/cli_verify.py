"""``trend-agent verify ...`` commands (anti-hallucination and evidence verification layer).

Every recommendation passes the verification gate before it is shown: claims are checked
against the evidence registry, statistics come only from the detection engine, and the
publish/withhold decision is made by application code. Verification reduces, but does
not eliminate, the risk of inaccurate statements.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.verification import RecommendationDraft, VerifiedRecommendation
from trend_agent.verification.config import load_verification_config
from trend_agent.verification.gate import GateDecision

verify_app = typer.Typer(
    help="Evidence registry and recommendation verification gate.",
    no_args_is_help=True,
)
NOTE = (
    "Verification checks claims against stored evidence with deterministic rules; it "
    "reduces, but does not eliminate, the risk of inaccurate statements."
)
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Allow synthetic (demo/test) data.")
]


def _service() -> tuple[Any, Any]:
    container = base._container(require_db=True)
    try:
        return container, container.verification()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"verification configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def _print_decision(decision: GateDecision) -> None:
    rec = decision.recommendation
    colour = {"publish": "green", "publish_with_labels": "yellow", "withhold": "red"}[
        rec.publication.value
    ]
    base.console.print(
        f"[bold]{rec.verification_status.value}[/] -> [{colour}]{rec.publication.value}[/] "
        f"(run {rec.run_id[:8]}, generator {escape(rec.generator)}, {rec.attempts} attempt(s))"
    )
    if rec.proposed_video_title:
        revised = " [dim](deterministic fallback)[/]" if rec.title_revised else ""
        base.console.print(f"title: {escape(rec.proposed_video_title)}{revised}")
    base.console.print(f"trend: {escape(rec.trend_statement)}")
    if rec.factual_claims:
        table = Table("status", "claim", "evidence")
        for c in rec.factual_claims:
            label = f"\n[dim]{escape(c.uncertainty_label)}[/]" if c.uncertainty_label else ""
            table.add_row(c.status.value, escape(c.text) + label, ", ".join(c.evidence_ids))
        base.console.print(table)
    if rec.removed_statements:
        base.console.print("[bold]removed:[/]")
        for r in rec.removed_statements:
            reason = r.reasons[0] if r.reasons else r.status.value
            base.console.print(f"  - {escape(r.text[:120])} [dim]({escape(reason)})[/]")
    if rec.missing_evidence:
        base.console.print("[bold]missing evidence:[/]")
        for m in rec.missing_evidence:
            base.console.print(f"  - {escape(m)}")
    base.console.print(f"[dim]{NOTE}[/]")


@verify_app.command("recommend")
def verify_recommend(
    target: Annotated[int, typer.Option("--target", "-t", help="Monitoring target id.")],
    generator: Annotated[
        str, typer.Option("--generator", "-g", help="template (offline) | anthropic")
    ] = "",
    include_synthetic: SyntheticOpt = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Do not persist the run.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Assemble evidence for a target, generate a draft and run the verification gate."""
    from trend_agent.verification.service import make_generator

    container, svc = _service()
    try:
        name = generator or svc.config.generator.default
        try:
            gen = make_generator(name, container.settings, svc.loaded)
        except ValueError as exc:
            raise base._fail(str(exc), 2) from None
        try:
            decision = svc.recommend(
                target, gen, include_synthetic=include_synthetic, persist=not dry_run
            )
        except (LookupError, PermissionError) as exc:
            raise base._fail(str(exc), 2) from None
    finally:
        _close(container)
    if as_json:
        base._emit_json(decision.recommendation.model_dump(mode="json"))
        return
    _print_decision(decision)


@verify_app.command("check")
def verify_check(
    target: Annotated[int, typer.Option("--target", "-t", help="Monitoring target id.")],
    draft: Annotated[Path, typer.Option("--draft", help="JSON file with a recommendation draft.")],
    include_synthetic: SyntheticOpt = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Verify an externally written draft (one attempt, no regeneration)."""
    try:
        raw = draft.read_text(encoding="utf-8")
    except OSError as exc:
        raise base._fail(f"cannot read {draft}: {exc}", 2) from None
    container, svc = _service()
    try:
        try:
            decision = svc.check(
                target, raw, include_synthetic=include_synthetic, persist=not dry_run
            )
        except (LookupError, PermissionError) as exc:
            raise base._fail(str(exc), 2) from None
    finally:
        _close(container)
    if as_json:
        base._emit_json(decision.recommendation.model_dump(mode="json"))
        return
    _print_decision(decision)


@verify_app.command("runs")
def verify_runs(
    target: Annotated[int | None, typer.Option("--target", "-t")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 20,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """List recent verification decisions."""
    container, svc = _service()
    try:
        rows = svc.runs(target_id=target, limit=limit, include_synthetic=include_synthetic)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table("run", "decided", "target", "state", "publication", "attempts", "title")
    for r in rows:
        table.add_row(
            r["run_id"][:8],
            r["decided_at"][:16],
            str(r["target_id"] or "-"),
            r["state"],
            r["publication"],
            str(r["attempts"]),
            escape((r["title"] or "-")[:60]),
        )
    base.console.print(table)


@verify_app.command("explain")
def verify_explain(
    run_id: Annotated[str, typer.Argument(help="Verification run id (full UUID).")],
    as_json: base.JsonOpt = False,
) -> None:
    """Show the gate steps and every claim verdict of one run."""
    container, svc = _service()
    try:
        try:
            data = svc.explain(run_id)
        except ValueError:
            raise base._fail("run id must be a UUID", 2) from None
    finally:
        _close(container)
    if data is None:
        raise base._fail(f"verification run {run_id} not found", 1)
    if as_json:
        base._emit_json(data)
        return
    base.console.print(f"[bold]{data['state']}[/] -> {data['publication']}")
    for s in data["steps"]:
        base.console.print(f"  {s['name']:<26} {s['outcome']:<8} {escape(s['detail'][:110])}")
    table = Table("att", "field", "status", "action", "claim", "why")
    for c in data["claims"]:
        table.add_row(
            str(c["attempt"]),
            c["field"],
            c["status"],
            c["action"],
            escape(c["text"][:70]),
            escape((c["reasons"] or [""])[0][:70]),
        )
    base.console.print(table)


@verify_app.command("evidence")
def verify_evidence(
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Evidence key prefix, e.g. surge_event:")
    ] = None,
    evidence_id: Annotated[
        str | None, typer.Option("--id", help="Show and integrity-check one record.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 30,
    as_json: base.JsonOpt = False,
) -> None:
    """List registry entries, or verify one record's signature and content hash."""
    container, svc = _service()
    try:
        if evidence_id:
            rec, problem = svc.verify_record(evidence_id)
            records = [rec] if rec else []
        else:
            problem = None
            records = svc.evidence(prefix=prefix, limit=limit)
    finally:
        _close(container)
    if evidence_id and not records:
        raise base._fail(f"evidence {evidence_id}: {problem}", 1)
    if as_json:
        out = [r.model_dump(mode="json") for r in records]
        if evidence_id:
            out[0]["integrity"] = problem or "ok"
        base._emit_json(out)
        return
    table = Table("evidence id", "key", "v", "kind", "trust", "semantics", "published", "flags")
    for r in records:
        table.add_row(
            r.evidence_id,
            escape(r.evidence_key[:40]),
            str(r.version),
            r.evidence_kind.value,
            r.trust_level.value,
            r.measurement_semantics.value,
            r.published_at.strftime("%Y-%m-%d") if r.published_at else "-",
            ",".join(r.injection_flags) or "",
        )
    base.console.print(table)
    if evidence_id:
        base.console.print(f"integrity: {problem or 'ok'}")


@verify_app.command("stats")
def verify_stats(as_json: base.JsonOpt = False) -> None:
    """Registry and gate counters."""
    container, svc = _service()
    try:
        data = svc.stats()
    finally:
        _close(container)
    if as_json:
        base._emit_json(data)
        return
    for section, counts in data.items():
        base.console.print(
            f"[bold]{section}[/]: " + ", ".join(f"{k}={v}" for k, v in counts.items())
        )


@verify_app.command("evaluate")
def verify_evaluate(
    split: Annotated[str, typer.Option("--split", help="dev | heldout | all")] = "all",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Run the labelled adversarial evaluation offline (no database, no network)."""
    from trend_agent.verification.evaluation import evaluate, to_json

    root = resolve_config_dir(None)
    loaded = load_verification_config(root)
    names = {"dev": ["cases_dev.yaml"], "heldout": ["cases_heldout.yaml"]}.get(
        split, ["cases_dev.yaml", "cases_heldout.yaml"]
    )
    results = evaluate(
        loaded,
        world_path=root / "verification" / "world.yaml",
        case_paths=[root / "verification" / n for n in names],
    )
    if output is not None:
        output.write_text(to_json(results) + "\n", encoding="utf-8")
    if as_json:
        base._emit_json(results)
        return
    table = Table("metric", *results["splits"].keys(), "overall")
    keys = [
        "cases",
        "labelled_claims",
        "unsupported_claim_rate_before_gate",
        "unsupported_claim_rate_published",
        "verification_precision",
        "false_rejection_rate",
        "contradiction_recall",
        "abstention_state_accuracy",
        "publication_accuracy",
        "over_abstention_rate",
        "under_abstention_rate",
        "injection_resistance",
        "forbidden_output_leaks",
    ]
    for k in keys:
        row = [str(results["splits"][s].get(k)) for s in results["splits"]]
        table.add_row(k, *row, str(results["overall"].get(k)))
    base.console.print(table)
    for f in results["failures"]:
        base.console.print(
            f"[yellow]{f['split']} {f['case']}[/]: {escape('; '.join(f['problems']))}"
        )
    base.console.print(
        "[dim]Labelled cases are finite and fictional; passing them does not show that "
        "hallucinations cannot occur.[/]"
    )


@verify_app.command("schema")
def verify_schema(
    which: Annotated[str, typer.Option("--which", help="draft | recommendation | llm")] = "draft",
) -> None:
    """Print a JSON schema: the generator's draft, the published contract, or the
    structured-output subset sent to an LLM."""
    if which == "recommendation":
        data = VerifiedRecommendation.model_json_schema()
    elif which == "llm":
        from trend_agent.verification.generation import llm_schema

        data = llm_schema()
    else:
        data = RecommendationDraft.model_json_schema()
    typer.echo(json.dumps(data, indent=2))


@verify_app.command("demo")
def verify_demo(as_json: base.JsonOpt = False) -> None:
    """Run a few labelled cases offline and show what the gate published or withheld."""
    from trend_agent.verification.evaluation import (
        _fixed,
        load_cases,
        run_case_with_decision,
    )
    from trend_agent.verification.gate import VerificationGate
    from trend_agent.verification.world import load_world

    root = resolve_config_dir(None)
    loaded = load_verification_config(root)
    _, cases = load_cases(root / "verification" / "cases_dev.yaml")
    picks = ["D01", "D02", "D07", "D13", "D19", "D23"]
    shown = []
    for case in [c for c in cases if c["id"] in picks]:
        world = load_world(root / "verification" / "world.yaml", loaded)
        gate = VerificationGate(world.registry, loaded, clock=_fixed(world.now))
        _, decision = run_case_with_decision(case, world, gate, "demo")
        shown.append((case, decision))
    if as_json:
        base._emit_json(
            [
                {
                    "case": c["id"],
                    "category": c["category"],
                    **d.recommendation.model_dump(mode="json"),
                }
                for c, d in shown
            ]
        )
        return
    for case, decision in shown:
        base.console.rule(f"{case['id']} - {case['category']}")
        _print_decision(decision)
