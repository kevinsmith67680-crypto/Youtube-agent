"""``trend-agent evaluate ...``: pipeline, detection, forecasting, content-gaps,
recommendations, replay and ground-truth labels. Every result states its data origin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import resolve_config_dir

evaluate_app = typer.Typer(help="Phase 8 evaluation (synthetic offline + real-label status).")

SizeOpt = Annotated[str, typer.Option("--size", help="small | full")]
OutOpt = Annotated[Path | None, typer.Option("--out", help="Write the JSON report here.")]
NoDbOpt = Annotated[bool, typer.Option("--no-db", help="Do not record experiments.")]


def _factory(no_db: bool) -> tuple[Any, Any]:
    if no_db:
        return None, None
    container = base._container(require_db=True)
    return container, container.session_factory


def _done(container: Any) -> None:
    if container is not None:
        base._run(container, base._noop)


def _write(out: Path | None, data: Any) -> None:
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


@evaluate_app.command("pipeline")
def pipeline(
    size: SizeOpt = "small",
    forecast_scale: Annotated[float, typer.Option(min=0.1, max=1.0)] = 0.4,
    out: OutOpt = None,
    no_db: NoDbOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Every component, pipeline integrity, backtest validity and the quality scorecard."""
    from trend_agent.evaluation.pipeline import run

    if size not in ("small", "full"):
        raise base._fail("--size must be small or full", 2)
    container, factory = _factory(no_db)
    try:
        rep = run(
            resolve_config_dir(None),
            factory=factory,
            now=_now(),
            size=size,
            forecast_scale=forecast_scale,
        )
    finally:
        _done(container)
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    card = rep["scorecard"]
    table = Table("component", "offline (synthetic)", "real world", "key results")
    for comp, c in card["components"].items():
        key = ", ".join(f"{k}={v['value']}" for k, v in c.get("checks", {}).items())
        table.add_row(comp, c["offline"], c["real_world"].get("status", "UNKNOWN"), escape(key))
    base.console.print(table)
    base.console.print(
        f"pipeline integrity: {card['pipeline_integrity']}; real-world accuracy: "
        f"{card['real_world_accuracy']}"
    )
    if "backtest_validity" in rep["results"]:
        base.console.print(f"backtest validity: {rep['results']['backtest_validity']['by_class']}")
    base.console.print(f"[dim]{card['notice']}[/]")
    base.console.print(f"timings (s): {rep['timings_seconds']}")


@evaluate_app.command("detection")
def detection_cmd(
    size: SizeOpt = "small",
    tune: Annotated[
        bool, typer.Option("--tune", help="Optimisation loop (dev split only).")
    ] = False,
    out: OutOpt = None,
    no_db: NoDbOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Precision, recall, false alerts/day and delays (overall and per source); with --tune,
    a bounded parameter search on dev with one held-out comparison on test."""
    from trend_agent.detection.config import load_detection_config
    from trend_agent.evaluation.detection import evaluate_synthetic
    from trend_agent.evaluation.registry import record
    from trend_agent.evaluation.replay import real_detection

    container, factory = _factory(no_db)
    try:
        rep = evaluate_synthetic(
            load_detection_config(resolve_config_dir(None)), now=_now(), size=size, tune=tune
        )
        rep["real_labels"] = real_detection(factory) if factory else {"status": "UNKNOWN"}
        t = rep.get("tuning")
        if t:
            rep["experiment"] = str(
                record(
                    factory,
                    name="detection tuning",
                    component="detection",
                    hypothesis="a (cusum h, fdr q) setting improves held-out F1 without more false "
                    "alerts",
                    config={"grid": [x["params"] for x in t["trials"]]},
                    dataset={"splits": rep["splits"]},
                    data_origin="synthetic",
                    split="test",
                    metrics=t.get("selected_test") or {},
                    baseline_metrics=rep["current"]["test"],
                    decision=t["decision"] if t["decision"] != "inconclusive" else "inconclusive",
                    decision_reason=t["reason"],
                )
            )
    finally:
        _done(container)
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    table = Table("set", "precision", "recall", "F1", "FA/1000 series-day", "delay p50", "p90")
    rows = [("test (current)", rep["current"]["test"]), ("dev (current)", rep["current"]["dev"])]
    rows += [(f"test: {k}", v) for k, v in rep["by_source_test"].items()]
    for name, m in rows:
        table.add_row(
            name,
            str(m["precision"]),
            str(m["recall"]),
            str(m["f1"]),
            str(m["false_alerts_per_1000_series_day"]),
            str(m["delay_median_days"]),
            str(m["delay_p90_days"]),
        )
    base.console.print(table)
    if rep.get("tuning"):
        t = rep["tuning"]
        base.console.print(f"tuning decision: {t['decision']} - {escape(t['reason'])}")
    base.console.print(f"real labels: {rep['real_labels']}")
    base.console.print("[dim]SYNTHETIC data: not real-world accuracy.[/]")


@evaluate_app.command("forecasting")
def forecasting_cmd(
    scale: Annotated[float, typer.Option(min=0.1, max=1.0)] = 0.4,
    out: OutOpt = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Per-horizon comparison against simple baselines (synthetic corpus)."""
    from trend_agent.evaluation.forecasting import evaluate

    rep = evaluate(resolve_config_dir(None), scale=scale)
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    table = Table("scope", "status", "selected", "metric", "value", "baseline", "beats", "LightGBM")
    for s in rep["scopes"]:
        table.add_row(
            s["scope"],
            s.get("status", ""),
            str(s.get("selected") or "-"),
            str(s.get("metric") or "-"),
            str(s.get("selected_value") or "-"),
            f"{s.get('baseline') or '-'} {s.get('baseline_value') or ''}",
            str(s.get("beats_baseline", "-")),
            str(s.get("lightgbm_meaningful", "-") if s.get("lightgbm_selected") else "-"),
        )
    base.console.print(table)
    base.console.print(f"summary: {rep['summary']}")


@evaluate_app.command("content-gaps")
def content_gaps_cmd(out: OutOpt = None, as_json: base.JsonOpt = False) -> None:
    """VERIFIED_GAP / CANDIDATE_GAP / NO_VERIFIABLE_GAP / INSUFFICIENT_EVIDENCE accuracy."""
    from trend_agent.evaluation.content_gaps import evaluate

    rep = evaluate(resolve_config_dir(None))
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    for split, s in rep["splits"].items():
        base.console.print(
            f"[bold]{split}[/] ({s['cases']} cases): predicted {s['predicted_distribution']}; "
            f"gap precision {s['gap_precision']}, recall {s['gap_recall']}, false gap rate "
            f"{s['false_gap_rate']}, false VERIFIED_GAP {s['false_verified_gaps']}"
        )
    base.console.print(f"[dim]{rep['notice']}[/]")


@evaluate_app.command("recommendations")
def recommendations_cmd(
    out: OutOpt = None, no_db: NoDbOpt = False, as_json: base.JsonOpt = False
) -> None:
    """Rule-based quality on held-out fixtures, plus descriptive real outcomes."""
    from trend_agent.evaluation.recommendations import evaluate_offline, outcome_summary

    container, factory = _factory(no_db)
    try:
        rep = evaluate_offline(resolve_config_dir(None))
        rep["real_outcomes"] = outcome_summary(factory)
    finally:
        _done(container)
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    base.console.print(json.dumps(rep, indent=2, default=str))


@evaluate_app.command("replay")
def replay_cmd(
    include_synthetic: Annotated[bool, typer.Option("--include-synthetic")] = False,
    out: OutOpt = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Walk-forward backtest validity of stored history (VALID / LIMITED / INVALID) and
    stored live alerts scored against real labels."""
    from trend_agent.evaluation.replay import real_detection, validity

    container = base._container(require_db=True)
    try:
        rep = {
            "validity": validity(container.session_factory, include_synthetic=include_synthetic),
            "real_detection": real_detection(container.session_factory),
        }
    finally:
        _done(container)
    _write(out, rep)
    if as_json:
        base._emit_json(rep)
        return
    v = rep["validity"]
    base.console.print(f"series {v['series']}: {v['by_class']} (reportable {v['reportable']})")
    base.console.print(f"real detection: {rep['real_detection']}")


labels_app = typer.Typer(help="Ground-truth labels (REAL and SYNTHETIC kept apart).")
evaluate_app.add_typer(labels_app, name="labels")


@labels_app.command("import")
def labels_import(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    origin: Annotated[str, typer.Option("--origin", help="real | synthetic")],
    as_json: base.JsonOpt = False,
) -> None:
    """Import JSON Lines labels with provenance; invalid lines are rejected with a reason."""
    from trend_agent.evaluation.labels import import_file

    container = base._container(require_db=True)
    try:
        rep = import_file(container.session_factory, path, origin=origin)
    except ValueError as exc:
        raise base._fail(str(exc), 2) from None
    finally:
        _done(container)
    if as_json:
        base._emit_json(rep.as_dict())
        return
    base.console.print(
        f"{rep.accepted} accepted, {rep.duplicates} duplicates, "
        f"{len(rep.rejected)} rejected ({origin})"
    )
    for r in rep.rejected[:20]:
        base.console.print(f"  line {r['line']}: {escape(r['reason'])}")


@labels_app.command("stats")
def labels_stats(as_json: base.JsonOpt = False) -> None:
    """Label counts by origin and kind."""
    from trend_agent.db.repositories.ops import LabelRepository
    from trend_agent.db.session import transaction

    container = base._container(require_db=True)
    with transaction(container.session_factory) as s:
        counts = LabelRepository(s).counts()
    _done(container)
    if as_json:
        base._emit_json(counts)
        return
    for origin, kinds in counts.items():
        base.console.print(f"{origin}: {kinds or 'none'}")
