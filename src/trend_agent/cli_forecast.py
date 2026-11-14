"""``trend-agent forecast ...`` commands (Phase 6: predictive trend forecasting).

Forecasts describe *relative* interest in the measured source, with explicit uncertainty
and abstention. Models trained on synthetic data are labelled synthetic and never serve
real series. Nothing here predicts video views or revenue.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.forecast import ForecastOutput

forecast_app = typer.Typer(help="Predictive trend forecasting (Phase 6).", no_args_is_help=True)
NOTE = (
    "Forecasts are relative-interest estimates with stated uncertainty, not search counts "
    "or view predictions. Synthetic models say nothing about real-world accuracy."
)
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Include synthetic (demo/test) series.")
]
DatasetOpt = Annotated[str, typer.Option("--dataset", help="database | synthetic")]
WorkersOpt = Annotated[int, typer.Option("--workers", min=1, max=16, help="Dataset workers.")]
DEFAULT_WORKERS = max(1, min(4, os.cpu_count() or 1))


def _hours(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().lower().removesuffix("h")
    if not v.isdigit():
        raise base._fail(f"horizon must look like 24h, got {value!r}", 2)
    return int(v)


def _service() -> tuple[Any, Any]:
    container = base._container(require_db=True)
    try:
        return container, container.forecasting()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"forecasting configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def _offline() -> tuple[Any, Any]:
    from trend_agent.detection.config import load_detection_config
    from trend_agent.forecasting.config import load_forecasting_config

    root = resolve_config_dir(None)
    return load_forecasting_config(root), load_detection_config(root)


def print_forecast(o: ForecastOutput) -> None:
    syn = " [magenta](synthetic)[/]" if o.synthetic else ""
    proxy = " [yellow](proxy attention)[/]" if o.is_proxy else ""
    base.console.print(
        f"[bold]{escape(o.query)}[/]{syn}{proxy} {o.forecast_horizon_hours} h -> "
        f"[bold]{o.status.value}[/] ({o.source_classification.value}, "
        f"{o.data_quality.status.value})"
    )
    for r in o.status_reasons:
        base.console.print(f"  [dim]reason: {escape(r)}[/]")
    if o.breakout is not None:
        b = o.breakout
        if not b.applicable:
            base.console.print("  breakout: not applicable (already elevated)")
        elif b.probability is not None:
            iv = (
                f" [{b.probability_interval[0]:.2f}-{b.probability_interval[1]:.2f}]"
                if (b.probability_interval)
                else ""
            )
            base.console.print(
                f"  breakout probability {b.probability:.3f}{iv} "
                f"({b.calibration_status.value}; threshold {b.threshold_index})"
            )
        else:
            score = f" score {b.score:.3f} (not a probability)" if b.score is not None else ""
            base.console.print(f"  breakout: {b.calibration_status.value}{score}")
    if o.continuation is not None and o.continuation.probability is not None:
        base.console.print(f"  continuation probability {o.continuation.probability:.3f}")
    for p in o.trajectory:
        rng = f" [{p.lower}-{p.upper}] ({p.interval_method})" if p.lower is not None else ""
        base.console.print(f"  trajectory +{p.lead_hours} h: {p.value}{rng} via {escape(p.model)}")
    if o.peak_estimate is not None:
        pe = o.peak_estimate
        span = f" {pe.earliest_hours}-{pe.latest_hours} h" if pe.earliest_hours is not None else ""
        base.console.print(
            f"  peak (experimental): {pe.status.value}{span} "
            f"P(beyond window)={pe.probability_beyond_horizon} ({pe.analogs} analogs)"
        )
    for w in o.opportunity_windows:
        pv = (
            "n/a"
            if w.probability_elevated_at_publication is None
            else (f"{w.probability_elevated_at_publication:.2f}")
        )
        base.console.print(
            f"  window {w.production_profile:<17} {w.status.value:<15} "
            f"P(elevated at publication)={pv}"
        )


@forecast_app.command("eligibility")
def forecast_eligibility(
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Which monitored series have enough comparable data for which horizons."""
    container, svc = _service()
    try:
        rows = svc.eligibility(category=category, include_synthetic=include_synthetic)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table(
        "series", "query", "measurement", "gran", *[f"{h}h" for h in svc.cfg.horizons_hours]
    )
    for r in rows:
        table.add_row(
            str(r["series_id"]),
            escape(r["query"][:36]),
            r["measurement"],
            r["granularity"],
            *[r["horizons"][str(h)]["status"] for h in svc.cfg.horizons_hours],
        )
    base.console.print(table)
    if not rows:
        base.console.print(
            "no monitored forecastable series (add --include-synthetic for demo data)"
        )


@forecast_app.command("features")
def forecast_features(
    query: Annotated[str, typer.Option("--query", "-q")],
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """The point-in-time feature vector of a query's series, with provenance."""
    container, svc = _service()
    try:
        rows = svc.features(query, include_synthetic=include_synthetic)
    finally:
        _close(container)
    if not rows:
        raise base._fail(f"no monitored series for query {query!r}", 1)
    if as_json:
        base._emit_json(rows)
        return
    for r in rows:
        base.console.rule(
            f"series {r['series_id']} {r['measurement']} {r['granularity']} @ {r['as_of']}"
        )
        table = Table("feature", "value", "unit", "source")
        for f in r["features"]:
            table.add_row(
                f["name"],
                "NaN (missing)" if f["value"] is None else str(f["value"]),
                f["unit"],
                f["source"],
            )
        base.console.print(table)
        for k, v in r["provenance"].items():
            base.console.print(f"  [dim]{k}: {escape(str(v))}[/]")
        for x in r["rejected"]:
            base.console.print(f"  [yellow]rejected:[/] {escape(x)}")


@forecast_app.command("train")
def forecast_train(
    horizon: Annotated[
        str | None, typer.Option("--horizon", help="e.g. 24h (default: all)")
    ] = None,
    dataset: DatasetOpt = "database",
    include_synthetic: SyntheticOpt = False,
    promote: Annotated[
        bool, typer.Option("--promote", help="Explicitly promote validated models.")
    ] = False,
    scale: Annotated[float, typer.Option("--scale", min=0.05, max=4.0)] = 1.0,
    workers: WorkersOpt = DEFAULT_WORKERS,
    as_json: base.JsonOpt = False,
) -> None:
    """Build a causal dataset, train and register models (VALIDATED / EXPERIMENTAL)."""
    h = _hours(horizon)
    container, svc = _service()
    try:
        res = svc.train(
            dataset=dataset,
            horizons=(h,) if h else None,
            include_synthetic=include_synthetic,
            promote=promote,
            workers=workers,
            scale=scale,
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json(res)
        return
    if res["status"] != "completed":
        raise base._fail(res.get("message", "training failed"), 1)
    m = res["manifest"]
    base.console.print(
        f"run {res['run_id'][:8]}: {m['rows']} rows, dataset {m['dataset_version']}, "
        f"origin {res['data_origin']}, leakage audit {m['leakage_audit']['mismatches']}"
        f"/{m['leakage_audit']['checked']} mismatches"
    )
    table = Table("scope", "selected", "candidates")
    for s in res["scopes"]:
        scope = (
            f"{s.get('family')}/{s.get('granularity')}/{s.get('horizon_hours')}h/{s.get('task')}"
        )
        cands = ", ".join(
            f"{c['name']}{'*' if c['validated'] else ''}" for c in s.get("candidates", [])
        )
        table.add_row(scope, str(s.get("selected")), escape(cands or "; ".join(s.get("notes", []))))
    base.console.print(table)
    for d in res["promotions"]:
        base.console.print(f"promoted: {d.get('promoted') or '-'} (scope {d['scope']})")
    if not promote:
        base.console.print("[dim]nothing promoted (use --promote; promotion is always explicit)[/]")
    base.console.print(f"[dim]{NOTE}[/]")


@forecast_app.command("evaluate")
def forecast_evaluate(
    horizon: Annotated[str | None, typer.Option("--horizon")] = None,
    dataset: DatasetOpt = "synthetic",
    include_synthetic: SyntheticOpt = False,
    scale: Annotated[float, typer.Option("--scale", min=0.05, max=4.0)] = 0.5,
    workers: WorkersOpt = DEFAULT_WORKERS,
    as_json: base.JsonOpt = False,
) -> None:
    """Re-score champions on the untouched chronological test period of a fresh dataset."""
    container, svc = _service()
    try:
        rows = svc.evaluate(
            horizon=_hours(horizon),
            dataset=dataset,
            include_synthetic=include_synthetic,
            scale=scale,
            workers=workers,
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table("scope", "rows", "brier", "pr_auc", "precision@50", "ece", "lead h")
    for r in rows:
        m = r["metrics"]
        table.add_row(
            r["scope"],
            str(r["test_rows"]),
            str(m.get("brier")),
            str(m.get("pr_auc")),
            str(m.get("precision_at_50")),
            str(m.get("ece")),
            str((m.get("lead_time") or {}).get("mean_lead_hours")),
        )
    base.console.print(table)
    if not rows:
        base.console.print("no champion models to evaluate (train with --promote first)")


@forecast_app.command("predict")
def forecast_predict(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    include_synthetic: SyntheticOpt = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Forecast eligible series (idempotent: an identical forecast is stored once)."""
    container, svc = _service()
    try:
        rows = svc.predict(
            query=query, category=category, include_synthetic=include_synthetic, persist=not dry_run
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json([{"created": c, **o.model_dump(mode="json")} for o, c in rows])
        return
    if not rows:
        raise base._fail("no monitored series matched", 1)
    for o, created in rows:
        print_forecast(o)
        base.console.print(
            f"  [dim]prediction {o.prediction_id} ({
                'stored' if created else 'already stored' if not dry_run else 'not stored'
            })[/]"
        )
    base.console.print(f"[dim]{NOTE}[/]")


@forecast_app.command("models")
def forecast_models(as_json: base.JsonOpt = False) -> None:
    """Registered models, scopes, data origin and validation state."""
    container, svc = _service()
    try:
        models = svc.models()
    finally:
        _close(container)
    if as_json:
        base._emit_json([m.model_dump(mode="json", exclude={"feature_names"}) for m in models])
        return
    table = Table("model", "type", "scope", "origin", "status", "brier", "mae")
    for m in models:
        table.add_row(
            m.model_id[-22:],
            m.model_type.value,
            f"{m.target_family.value}/{m.granularity}/{m.horizon_hours}h/{m.target_type.value}",
            m.data_origin.value,
            m.status.value,
            str((m.metrics or {}).get("brier")),
            str((m.metrics or {}).get("mae")),
        )
    base.console.print(table)


@forecast_app.command("compare")
def forecast_compare(
    dataset: DatasetOpt = "synthetic",
    scale: Annotated[float, typer.Option("--scale", min=0.05, max=4.0)] = 0.5,
    as_json: base.JsonOpt = False,
) -> None:
    """Champion vs challengers on the same held-out rows (explicit criteria)."""
    container, svc = _service()
    try:
        rows = svc.compare(dataset=dataset, scale=scale)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    for r in rows:
        if not r.get("comparable"):
            base.console.print(f"[yellow]not comparable:[/] {r.get('reason')}")
            continue
        base.console.print(
            f"{r['champion']['name']} vs {r['challenger']['name']}: "
            f"promote={r['promote']} criteria={r['criteria']}"
        )
    if not rows:
        base.console.print("no champion/challenger pairs")


@forecast_app.command("promote")
def forecast_promote(
    model_id: Annotated[str, typer.Argument()], as_json: base.JsonOpt = False
) -> None:
    """Explicitly promote a VALIDATED or CHALLENGER model to CHAMPION (logged)."""
    container, svc = _service()
    try:
        try:
            meta = svc.promote(model_id)
        except (LookupError, ValueError) as exc:
            raise base._fail(str(exc), 2) from None
    finally:
        _close(container)
    if as_json:
        base._emit_json({"model_id": meta.model_id, "status": meta.status.value})
        return
    base.console.print(f"{meta.model_id} is now {meta.status.value}")


@forecast_app.command("rollback")
def forecast_rollback(
    model_id: Annotated[str, typer.Argument()],
    reject: Annotated[
        bool, typer.Option("--reject", help="Mark it REJECTED (failed in service).")
    ] = False,
    restore_previous: Annotated[
        bool,
        typer.Option("--restore-previous", help="Reinstate the champion it replaced."),
    ] = False,
    reason: Annotated[str, typer.Option("--reason")] = "operator rollback",
) -> None:
    """Take a champion out of service (logged). Default: retire it, and its scope falls
    back to baseline-only forecasts."""
    container, svc = _service()
    try:
        try:
            restored = svc.rollback(
                model_id, reason, reject=reject, restore_previous=restore_previous
            )
        except LookupError as exc:
            raise base._fail(str(exc), 2) from None
    finally:
        _close(container)
    state = "rejected" if reject else "retired"
    if restored:
        base.console.print(f"{model_id} {state}; {restored} restored as champion")
    else:
        base.console.print(f"{model_id} {state}; baseline-only forecasts for its scope")


@forecast_app.command("reject")
def forecast_reject(
    model_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Mark a non-champion model REJECTED (never served or promoted; logged)."""
    container, svc = _service()
    try:
        try:
            svc.reject(model_id, reason)
        except LookupError as exc:
            raise base._fail(str(exc), 2) from None
    finally:
        _close(container)
    base.console.print(f"{model_id} rejected")


@forecast_app.command("explain")
def forecast_explain(
    prediction_id: Annotated[str | None, typer.Argument()] = None,
    prediction_opt: Annotated[str | None, typer.Option("--prediction-id")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Model evidence, feature contributions, calibration and limitations of a forecast."""
    ident = prediction_opt or prediction_id
    if not ident:
        raise base._fail("give a prediction id (argument or --prediction-id)", 2)
    container, svc = _service()
    try:
        out = svc.explain(ident)
    finally:
        _close(container)
    if out is None:
        raise base._fail(f"prediction {ident} not found", 1)
    if as_json:
        base._emit_json(out)
        return
    o = ForecastOutput.model_validate(out)
    print_forecast(o)
    e = o.explanation
    if e is not None:
        for label, text in (
            ("predicted", e.predicted_target),
            ("horizon", e.horizon),
            ("data", e.data_type),
            ("baseline comparison", e.baseline_comparison),
            ("calibration", e.calibration),
            ("uncertainty", e.uncertainty),
            ("validation", e.validation),
            ("opportunity", e.opportunity_reasoning),
        ):
            base.console.print(f"  [cyan]{label}[/]: {escape(text)}")
        for c in e.top_features:
            base.console.print(
                f"    {c.feature:<26} value={c.value} contribution={c.contribution:+.4f}"
            )
        base.console.print(f"  [dim]{escape(e.attribution_note)}[/]")
        for lim in e.limitations:
            base.console.print(f"  [dim]limitation: {escape(lim)}[/]")


@forecast_app.command("peaks")
def forecast_peaks(include_synthetic: SyntheticOpt = False, as_json: base.JsonOpt = False) -> None:
    """Stored peak-timing estimates (experimental; censoring explicit)."""
    container, svc = _service()
    try:
        rows = svc.peaks(include_synthetic=include_synthetic)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table("query", "h", "status", "window", "P(beyond)", "censoring")
    for r in rows:
        span = (
            f"{r['earliest_hours']}-{r['latest_hours']} h"
            if r["earliest_hours"] is not None
            else "-"
        )
        table.add_row(
            escape(str(r["query"])[:36]),
            str(r["horizon_hours"]),
            r["status"],
            span,
            str(r["probability_beyond_horizon"]),
            r["censoring"],
        )
    base.console.print(table)


@forecast_app.command("windows")
def forecast_windows(
    include_synthetic: SyntheticOpt = False,
    status: Annotated[str | None, typer.Option("--status")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Stored opportunity windows per production profile (planning assumptions)."""
    container, svc = _service()
    try:
        rows = svc.windows(include_synthetic=include_synthetic, status=status)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    table = Table("query", "h", "profile", "publication", "status", "P(elevated)")
    for r in rows:
        table.add_row(
            escape(str(r["query"])[:36]),
            str(r["horizon_hours"]),
            r["profile"],
            r["publication"][:16],
            r["status"],
            str(r["probability_elevated"]),
        )
    base.console.print(table)
    base.console.print(
        "[dim]Estimates of demand persistence, not predictions of video performance.[/]"
    )


@forecast_app.command("drift")
def forecast_drift(
    days: Annotated[int, typer.Option("--days", min=1, max=365)] = 7, as_json: base.JsonOpt = False
) -> None:
    """Data drift (feature PSI) and concept drift (matured labels) of champion models."""
    container, svc = _service()
    try:
        rows = svc.drift(since_days=days)
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    for r in rows:
        base.console.print(
            f"{r['scope']}: data={r['data'].get('status')} "
            f"concept={r['concept'].get('status')} -> {r['recommendation']['action']}"
            f" ({escape(r['recommendation']['reason'])})"
        )
    if not rows:
        base.console.print("no champion models")


@forecast_app.command("status")
def forecast_status(as_json: base.JsonOpt = False) -> None:
    """Registry and prediction counts."""
    container, svc = _service()
    try:
        data = svc.status()
    finally:
        _close(container)
    if as_json:
        base._emit_json(data)
        return
    for k, v in data.items():
        base.console.print(f"{k}: {v}")


@forecast_app.command("replay")
def forecast_replay(
    dataset: DatasetOpt = "synthetic",
    scale: Annotated[float, typer.Option("--scale", min=0.05, max=4.0)] = 0.25,
    step_hours: Annotated[int, typer.Option("--step-hours", min=1, max=168)] = 12,
    as_json: base.JsonOpt = False,
) -> None:
    """Causal forecasting replay through the Phase 3 replay engine (synthetic: offline)."""
    if dataset != "synthetic":
        raise base._fail(
            "only --dataset synthetic is supported offline; database replay uses "
            "stored champions via `forecast evaluate --dataset database`",
            2,
        )
    from datetime import timedelta

    from trend_agent.forecasting import replay_adapter, scenarios
    from trend_agent.forecasting import synthetic as syn

    loaded, dloaded = _offline()
    ctx = scenarios.prepare(
        loaded, dloaded, scale=scale, workers=max(1, min(4, os.cpu_count() or 1))
    )
    held = syn.materialise(
        syn.corpus(
            hourly=8,
            daily=0,
            hourly_days=loaded.config.corpus.hourly_days,
            daily_days=60,
            seed=loaded.config.corpus.seed + 99,
        )
    )
    start = syn.START + timedelta(days=10)
    end = syn.START + timedelta(days=loaded.config.corpus.hourly_days - 4)
    res = replay_adapter.run(
        ctx.engine, held, start=start, end=end, step=timedelta(hours=step_hours)
    )
    res["synthetic"] = True
    res["note"] = "SYNTHETIC replay with synthetic-trained models: behaviour check, not accuracy"
    if as_json:
        base._emit_json(res)
        return
    f = res["forecasts"]
    base.console.print(
        f"replay: {res['replay']['steps']} steps, {f['forecasts']} forecasts, "
        f"availability {f['availability']}, abstention {f['abstention_rate']}, "
        f"OOD {f['ood_rate']}"
    )
    base.console.print(
        f"scored probabilities: {f['scored_probabilities']} brier={f.get('brier')} "
        f"observed rate={f.get('observed_rate')} mean p={f.get('mean_probability')}"
    )
    base.console.print(f"latency per series step: {res['latency_ms']} ms")
    base.console.print(f"[dim]{res['note']}[/]")


@forecast_app.command("demo")
def forecast_demo(
    scenario: Annotated[list[str] | None, typer.Option("--scenario", "-s")] = None,
    scale: Annotated[float, typer.Option("--scale", min=0.05, max=4.0)] = 0.5,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Full Phase 6 pipeline on deterministic SYNTHETIC data (offline, no database)."""
    from trend_agent.forecasting import scenarios

    loaded, dloaded = _offline()
    ctx = scenarios.prepare(
        loaded, dloaded, scale=scale, workers=max(1, min(4, os.cpu_count() or 1))
    )
    results = scenarios.run(ctx, [s.upper() for s in scenario] if scenario else None)
    if as_json:
        base._emit_json(
            {
                "timings": ctx.timings,
                "dataset": {
                    k: ctx.dataset.manifest.get(k)
                    for k in ("dataset_version", "rows", "leakage_audit", "label_counts")
                },
                "promotions": ctx.decisions,
                "models": [
                    m.model_dump(mode="json", exclude={"feature_names"})
                    for m in ctx.registry.store.all()
                ],
                "scenarios": [
                    {
                        "scenario": r.key,
                        "name": r.name,
                        "passed": r.passed,
                        "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in r.checks],
                        "details": r.details,
                        "forecasts": [o.model_dump(mode="json") for o in r.outputs],
                    }
                    for r in results
                ],
            }
        )
        return
    m = ctx.dataset.manifest
    base.console.print(
        f"[bold]SYNTHETIC[/] corpus: {m['rows']} rows, leakage audit "
        f"{m['leakage_audit']['mismatches']}/{m['leakage_audit']['checked']} mismatches; "
        f"timings {ctx.timings}"
    )
    for d in ctx.decisions:
        base.console.print(f"  champion: {d.get('promoted')} ({d['scope']})")
    for r in results:
        base.console.rule(
            f"{r.key} - {escape(r.name)} {'[green]PASS[/]' if r.passed else '[red]FAIL[/]'}"
        )
        for name, ok, detail in r.checks:
            mark = "[green]ok[/]" if ok else "[red]FAILED[/]"
            base.console.print(
                f"  {mark} {escape(name)}"
                + (f" [dim]({escape(detail[:120])})[/]" if detail else "")
            )
        if verbose:
            for o in r.outputs[:3]:
                print_forecast(o)
    passed = sum(r.passed for r in results)
    base.console.print(
        f"\n{passed}/{len(results)} scenarios met their expectations. Synthetic, "
        "fictional data: this demonstrates behaviour, not real-world accuracy."
    )
    if passed != len(results):
        raise typer.Exit(1)


@forecast_app.command("schema")
def forecast_schema(
    which: Annotated[str, typer.Option("--which", help="output | features")] = "output",
) -> None:
    """Print the forecast output contract (JSON schema) or the feature schema."""
    if which == "features":
        from trend_agent.forecasting.feature_engineering import schema_document

        typer.echo(json.dumps(schema_document(), indent=2))
        return
    typer.echo(json.dumps(ForecastOutput.model_json_schema(), indent=2))
