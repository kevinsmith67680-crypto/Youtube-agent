"""``trend-agent detect ...`` commands (Phase 4: statistical surge detection).

A surge alert describes an increase in what a source *measures*. Direct YouTube search
interest is a relative index (never a number of searches); external search interest,
video activity, suggestions, official events and community activity are labelled as such
and can never become a confirmed YouTube search surge. Severity is a ranking index, not a
probability.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.db.repositories.detection_runs import DetectionRunRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.insights import DetectionInsights
from trend_agent.detection.replay_adapter import DetectionReplayer
from trend_agent.detection.service import DetectionService, load_nulls, target_info
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import Category
from trend_agent.monitoring.replay import AvailabilityMode, replay
from trend_agent.monitoring.scheduler import period_step
from trend_agent.schemas.detection import RunType
from trend_agent.schemas.evidence import AlertStage, classify, effective_category
from trend_agent.schemas.surge import SurgeAlert
from trend_agent.utils.text import canonicalize_query
from trend_agent.utils.time import ensure_utc

detect_app = typer.Typer(
    help="Statistical surge detection: runs, alerts, explanations, replay, evaluation.",
    no_args_is_help=True,
)
NOTE = (
    "Alerts describe increases in what each source measures; relative indices are not "
    "search counts, and severity is a ranking index, not a probability."
)
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Include synthetic (demo/test) data.")
]
CategoryOpt = Annotated[Category | None, typer.Option("--category", "-c")]


def _service(*, include_synthetic: bool | None = None) -> tuple[Any, DetectionService]:
    container = base._container(require_db=True)
    try:
        return container, container.detection(include_synthetic=include_synthetic)
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"detection configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def _fmt_time(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else str(value or "-")


# --------------------------------------------------------------------------- run
@detect_app.command("run")
def detect_run(
    all_: Annotated[bool, typer.Option("--all", help="All eligible series (default).")] = False,
    category: CategoryOpt = None,
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    from_queue: Annotated[
        bool, typer.Option("--from-queue", help="Only series queued by new observations.")
    ] = False,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Run detection for eligible observations (new or revised since the last run)."""
    container, svc = _service(include_synthetic=include_synthetic)
    try:
        report = svc.run(
            run_type=RunType.LIVE,
            category=category,
            query=canonicalize_query(query) if query else None,
            from_queue=from_queue,
            include_synthetic=include_synthetic,
        )
    finally:
        _close(container)
    data = report.as_dict()
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"detection run {data['run_id'][:8]}: {data['series_evaluated']} series, "
        f"{data['observations_evaluated']} new observation(s), {data['hypotheses_tested']} "
        f"calibrated hypotheses ({data['accepted_after_control']} accepted), "
        f"{data['notifications']} new alert(s), {len(data['errors'])} error(s) "
        f"in {data['seconds']}s"
    )
    if data["skipped"]:
        base.console.print(f"[dim]skipped: {data['skipped']}[/]")
    if data["changes"]:
        table = Table("event", "query", "cat", "class", "change", "stage", "severity", "alert")
        for ch in data["changes"]:
            table.add_row(
                str(ch["event_id"]),
                escape(ch["query"]),
                ch["category"],
                ch["classification"],
                ch["kind"],
                ch["stage"] or "-",
                f"{ch['severity']:.1f}",
                "yes" if ch["notification"] else "",
            )
        base.console.print(table)
    if not all_ and not category and not query and not from_queue:
        base.console.print("[dim]Scope: all eligible series (--all).[/]")
    base.console.print(f"[dim]{NOTE}[/]")
    if data["errors"]:
        raise typer.Exit(1)


@detect_app.command("worker")
def detect_worker(
    worker_id: Annotated[str, typer.Option("--id")] = "detect-worker",
    once: Annotated[bool, typer.Option("--once")] = False,
    fallback: Annotated[
        int | None, typer.Option("--fallback-seconds", min=1, help="Re-check interval.")
    ] = None,
    include_synthetic: SyntheticOpt = False,
) -> None:
    """Event-driven worker: LISTEN for new observations (Phase 3 outbox) and detect."""
    from trend_agent.detection.worker import run_worker

    container, svc = _service(include_synthetic=include_synthetic)
    integ = svc.cfg.integration
    base.console.print(
        f"detection worker {worker_id}: LISTEN {integ.notify_channel} "
        f"(fallback every {fallback or integ.worker_fallback_seconds}s); Ctrl-C to stop"
    )

    def show(report: Any) -> None:
        if report.series_evaluated or report.notifications:
            base.console.print(
                f"{_fmt_time(report.as_of)} {report.series_evaluated} series, "
                f"{report.notifications} new alert(s)"
            )

    try:
        run_worker(
            svc,
            container.engine,
            worker_id=worker_id,
            channel=integ.notify_channel,
            fallback_seconds=float(fallback or integ.worker_fallback_seconds),
            once=once,
            on_report=show,
        )
    except KeyboardInterrupt:
        base.console.print("stopped")
    finally:
        _close(container)


# --------------------------------------------------------------------------- inspection
@detect_app.command("query")
def detect_query(
    query: Annotated[str, typer.Option("--query", "-q")], as_json: base.JsonOpt = False
) -> None:
    """History, detector readiness, state and recent anomaly results for one query."""
    container, svc = _service()
    canonical = canonicalize_query(query)
    try:
        data = DetectionInsights(svc).query(canonical)
    finally:
        _close(container)
    if not data["targets"]:
        raise base._fail(f"no monitoring target for {canonical!r}", 1)
    if as_json:
        base._emit_json(data)
        return
    for t in data["targets"]:
        base.console.print(
            f"[bold]{escape(canonical)}[/] target {t['target_id']} ({t['category']}, "
            f"{t['measurement']}, {t['status']}{', SYNTHETIC' if t['synthetic'] else ''})"
        )
        for s in t["series"]:
            base.console.print(
                f"  series {s['series_id']} {s['provider']} [{s['classification']}] "
                f"preprocess={s['preprocess_status']} usable={s['usable_values']} "
                f"missing={s['missing_periods']}+{s['missing_values']} baseline="
                f"{s['baseline_model']}/{s['baseline_readiness']} eligible={s['eligible']}"
            )
            for r in s["reasons"][:4]:
                base.console.print(f"    [dim]- {escape(r)}[/]")
            if s.get("state"):
                st = s["state"]
                base.console.print(
                    f"    state: processed until {_fmt_time(st['processed_until'])}, "
                    f"CUSUM {st['cusum']} ({st['cusum_alarms']} alarms), "
                    f"{st['evaluations']} evaluations, {st['recomputations']} recomputations"
                )
            if s["results"]:
                table = Table("period", "detector", "status", "score", "thr", "p", "base", "obs")
                for r in s["results"][:12]:
                    table.add_row(
                        _fmt_time(r["period_start"]),
                        r["detector"],
                        r["status"],
                        str(r["score"]),
                        str(r["threshold"]),
                        f"{r['p_value']:.2g}" if r["p_value"] else "-",
                        str(r["baseline"]),
                        str(r["observed"]),
                    )
                base.console.print(table)
        for e in t["episodes"]:
            base.console.print(
                f"  episode {e['event_id']}: {e['status']} {e['classification']} "
                f"severity {e['severity']} (first detected {_fmt_time(e['first_detected_at'])})"
            )


@detect_app.command("alerts")
def detect_alerts(
    active: Annotated[bool, typer.Option("--active", help="Only open episodes.")] = False,
    category: CategoryOpt = None,
    status: Annotated[list[AlertStage] | None, typer.Option("--status")] = None,
    include_synthetic: SyntheticOpt = False,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 100,
    as_json: base.JsonOpt = False,
) -> None:
    """List surge episodes (production data only unless --include-synthetic)."""
    container, svc = _service()
    with transaction(svc.session_factory) as session:
        rows = SurgeEventRepository(session).events(
            active=active,
            statuses=status,
            category=category,
            include_synthetic=include_synthetic,
            limit=limit,
        )
        texts = MonitoringRepository(session).query_text(list({r.query_id for r in rows}))
        data: list[dict[str, Any]] = [
            {
                "event_id": r.id,
                "query": texts.get(r.query_id, ("?", "?"))[0],
                "category": r.category.value,
                "classification": r.evidence_classification.value,
                "measurement": r.measurement_category.value,
                "provider": r.provider_name,
                "status": r.status.value,
                "confidence": r.confidence_status.value,
                "severity": round(r.severity, 1),
                "maximum_severity": round(r.maximum_severity, 1),
                "episode_start": r.episode_start,
                "first_detected_at": r.first_detected_at,
                "last_updated_at": r.last_updated_at,
                "resolved_at": r.resolved_at,
                "synthetic": r.is_synthetic,
            }
            for r in rows
        ]
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    if not data:
        base.console.print(
            "No surge episodes" + ("" if include_synthetic else " (try --include-synthetic)") + "."
        )
        return
    table = Table(
        "event", "query", "cat", "class", "status", "confidence", "sev", "max", "detected"
    )
    for d in data:
        table.add_row(
            str(d["event_id"]),
            escape(d["query"]) + (" [yellow](synthetic)[/]" if d["synthetic"] else ""),
            d["category"],
            d["classification"],
            d["status"],
            d["confidence"],
            f"{d['severity']:.0f}",
            f"{d['maximum_severity']:.0f}",
            _fmt_time(d["first_detected_at"]),
        )
    base.console.print(table)
    base.console.print(f"[dim]{NOTE}[/]")


@detect_app.command("explain")
def detect_explain(
    event_id: Annotated[int, typer.Option("--event-id", "-e")], as_json: base.JsonOpt = False
) -> None:
    """Full evidence, reasoning, transitions and the versioned alert payload of an episode."""
    container, svc = _service()
    with transaction(svc.session_factory) as session:
        repo = SurgeEventRepository(session)
        row = repo.get(event_id)
        if row is None:
            _close(container)
            raise base._fail(f"surge event {event_id} not found", 1)
        transitions: list[dict[str, Any]] = [
            {
                "at": t.at,
                "kind": t.kind,
                "from": t.from_status,
                "to": t.to_status,
                "severity": round(t.severity, 1),
                "notification": t.is_notification,
                "reason": t.reason,
            }
            for t in repo.transitions(event_id)
        ]
        evidence = [
            {
                "type": e.evidence_type,
                "observation_id": e.observation_id,
                "detector_result_id": e.detector_result_id,
                "contribution": e.contribution,
                "source_reference": e.source_reference,
            }
            for e in repo.evidence(event_id)
        ]
        data = {
            "event_id": row.id,
            "status": row.status.value,
            "classification": row.evidence_classification.value,
            "explanation": row.explanation,
            "transitions": transitions,
            "evidence": evidence,
            "alert": row.payload,
        }
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    alert = row.payload
    base.console.print(
        f"[bold]Surge event {event_id}[/] - {data['status']} / {data['classification']}"
    )
    base.console.print(escape(row.explanation))
    if alert and "baseline" in alert:
        b = alert["baseline"]
        base.console.print(
            f"\nBaseline: {b['model']} ({b['model_version']}) expected {b['expected']} "
            f"(dispersion {b['dispersion']}, {b['dispersion_source']}), observed {b['observed']}, "
            f"n={b['sample_count']}, {b['readiness']}, scale {b['scale']} / {b['unit']}"
        )
        table = Table("detector", "version", "triggered", "status", "score", "threshold", "p")
        for d in alert.get("detectors", []):
            table.add_row(
                d["name"],
                d["version"],
                "yes" if d["triggered"] else "",
                d["status"],
                str(d["score"]),
                str(d["threshold"]),
                f"{d['p_value']:.2g}" if d.get("p_value") else "-",
            )
        base.console.print(table)
        mt = alert.get("multiple_testing") or {}
        base.console.print(
            f"Multiple testing: {mt.get('method')} family {mt.get('family_size')} accepted "
            f"{mt.get('accepted')} ({mt.get('note')})"
        )
        base.console.print(
            f"Confidence: {alert['confidence_status']}; severity {alert['severity']} "
            f"(max {alert['maximum_severity']}); comparability {alert['comparability']['status']}"
        )
        if alert.get("limitations"):
            base.console.print("Limitations: " + "; ".join(alert["limitations"]))
    table = Table("at", "change", "from", "to", "sev", "alert", "reason")
    for t in transitions:
        table.add_row(
            _fmt_time(t["at"]),
            t["kind"],
            t["from"] or "-",
            t["to"],
            str(t["severity"]),
            "yes" if t["notification"] else "",
            escape(t["reason"][:80]),
        )
    base.console.print(table)
    base.console.print(f"[dim]{len(evidence)} evidence link(s); {NOTE}[/]")


@detect_app.command("readiness")
def detect_readiness(
    category: CategoryOpt = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Which series are eligible for statistical detection, and why not."""
    container, svc = _service()
    try:
        rows = DetectionInsights(svc).readiness(
            category=category, include_synthetic=include_synthetic
        )
    finally:
        _close(container)
    if as_json:
        base._emit_json(rows)
        return
    if not rows:
        base.console.print(
            "No monitored series"
            + ("" if include_synthetic else " (try --include-synthetic)")
            + "."
        )
        return
    table = Table(
        "query", "provider", "class", "preprocess", "usable", "baseline", "eligible", "reason"
    )
    for r in rows:
        table.add_row(
            escape(r["query"]),
            r["provider"],
            r["classification"] or "-",
            r["preprocess_status"],
            str(r["usable_values"]),
            f"{r['baseline_model']}/{r['baseline_readiness']}",
            "yes" if r["eligible"] else "no",
            escape("; ".join(r["reasons"])[:70]),
        )
    base.console.print(table)
    eligible = sum(1 for r in rows if r["eligible"])
    base.console.print(f"{eligible}/{len(rows)} series eligible for statistical detection")


@detect_app.command("thresholds")
def detect_thresholds(as_json: base.JsonOpt = False) -> None:
    """Configured thresholds, versions and active calibrations."""
    container, svc = _service()
    try:
        data = DetectionInsights(svc).thresholds()
    finally:
        _close(container)
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"config {data['config_version']}, algorithm {data['algorithm_version']}, "
        f"thresholds {data['threshold_version']}"
    )
    a = data["anomaly"]
    base.console.print(
        f"anomaly: watch z {a['defaults']['watch_z']}, alert z {a['defaults']['alert_z']}, "
        f"min effect {a['defaults']['min_absolute_effect']}, min level "
        f"{a['defaults']['min_observed_level']}; overrides: "
        + ", ".join(f"{o['name']} {o['match']} -> {o['set']}" for o in a["overrides"])
    )
    c = data["cusum"]
    base.console.print(
        f"cusum: k {c['k']} h {c['h']} clip {c['z_clip']} target ARL0 {c['target_arl0']}"
    )
    cp = data["change_point"]
    base.console.print(
        f"change point: {cp['method']} hazard 1/{cp['hazard_lambda']} "
        f"p>={cp['probability_threshold']} shift>={cp['min_shift_sigma']} sd"
    )
    mt = data["multiple_testing"]
    base.console.print(
        f"multiple testing: {mt['method']} q={mt['fdr_q']}; budget "
        f"{mt['max_new_alerts_per_day']}/day; target false alerts "
        f"{mt['target_false_alerts_per_day']}/day"
    )
    if data["calibrations"]:
        table = Table("stratum", "version", "source", "n", "tail from", "GPD")
        for cal in data["calibrations"]:
            table.add_row(
                cal["stratum"],
                cal["version"],
                cal["source"],
                str(cal["n_total"]),
                str(cal["tail_threshold"]),
                "ok" if cal["gpd_ok"] else "no",
            )
        base.console.print(table)
    else:
        base.console.print(
            "[yellow]No calibration stored:[/] p-values are unavailable; alerts are "
            "EMPIRICALLY_THRESHOLDED and cannot be CONFIRMED (confirm_requires_calibration)."
        )


@detect_app.command("stats")
def detect_stats(include_synthetic: SyntheticOpt = False, as_json: base.JsonOpt = False) -> None:
    """Operational metrics and recent detection runs."""
    container, svc = _service()
    try:
        data = DetectionInsights(svc).stats(include_synthetic=include_synthetic)
    finally:
        _close(container)
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"queue depth {data['queue_depth']}; series with detector state "
        f"{data['series_with_state']}; production episodes {data['episodes']}; "
        f"notifications (24h) {data['notifications_last_24h']}"
    )
    if data.get("synthetic_episodes") is not None:
        base.console.print(f"synthetic episodes {data['synthetic_episodes']}")
    table = Table("started", "type", "status", "series", "obs", "hyp", "acc", "alerts", "err", "s")
    for r in data["recent_runs"]:
        table.add_row(
            _fmt_time(r["started_at"]),
            r["type"],
            r["status"],
            str(r["series"]),
            str(r["observations"]),
            str(r["hypotheses"]),
            str(r["accepted"]),
            str(r["alerts"]),
            str(r["errors"]),
            str(r["seconds"]),
        )
    base.console.print(table)
    if data["evaluations"]:
        for e in data["evaluations"]:
            base.console.print(
                f"evaluation {e['dataset']} ({e['split']}"
                f"{', synthetic' if e['synthetic'] else ''}): precision {e['precision']} recall {e['recall']} "
                f"false alerts/day {e['false_alerts_per_day']}"
            )


@detect_app.command("schema")
def detect_schema() -> None:
    """Print the JSON schema of the versioned SurgeAlert output contract (Phase 5)."""
    base._emit_json(SurgeAlert.model_json_schema())


# --------------------------------------------------------------------------- replay
@detect_app.command("replay")
def detect_replay(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    provider: Annotated[str | None, typer.Option("--provider", "-p")] = None,
    start: Annotated[datetime | None, typer.Option(help="Replay clock start (UTC).")] = None,
    end: Annotated[datetime | None, typer.Option(help="Replay clock end (UTC).")] = None,
    step_hours: Annotated[int, typer.Option(min=1, max=24 * 31)] = 24,
    mode: Annotated[AvailabilityMode, typer.Option()] = AvailabilityMode.PUBLICATION,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Causal detection replay: each step sees only data available at that time."""
    if query is None and provider is None:
        raise base._fail("give --query and/or --provider", 2)
    container, svc = _service()
    canonical = canonicalize_query(query) if query else None
    with transaction(svc.session_factory) as session:
        repo = MonitoringRepository(session)
        ts = TimeSeriesRepository(session)
        targets = repo.targets(
            query_text=canonical, include_synthetic=include_synthetic or bool(query), limit=500
        )
        texts = repo.query_text(list({t.query_id for t in targets}))
        nulls = load_nulls(session)
        points = []
        metas: dict[int, EpisodeMeta] = {}
        for t in targets:
            info = target_info(t, texts.get(t.query_id, ("?", "?"))[0], None)
            for p in ts.points(
                target_id=t.id, provider_name=provider, current_only=False, limit=1_000_000
            ):
                points.append(p)
                if p.series_id not in metas:
                    m = effective_category(p.measurement_category, p.simulated_category)
                    metas[p.series_id] = EpisodeMeta(
                        target_id=t.id,
                        query=info.query,
                        query_id=t.query_id,
                        keyword_id=t.keyword_id,
                        topic_cluster_id=t.topic_cluster_id,
                        category=t.category,
                        region=p.region,
                        language=p.language,
                        provider=p.provider_name,
                        measurement=m,
                        is_proxy=p.is_proxy,
                        classification=classify(m, is_proxy=p.is_proxy),
                        is_synthetic=p.is_synthetic,
                        step=period_step(p.granularity) or timedelta(days=1),
                    )
    _close(container)
    if not points:
        raise base._fail("no stored observations for this selection", 1)
    first = min(p.period_end for p in points)
    last = max(
        max(p.collected_at, p.ingested_at, p.provider_published_at or p.collected_at)
        for p in points
    )
    begin = ensure_utc(start) if start else first
    finish = ensure_utc(end) if end else last
    replayer = DetectionReplayer(svc.loaded, metas, nulls=nulls, mode=mode)
    report = replay(
        points,
        start=begin,
        end=finish,
        step=timedelta(hours=step_hours),
        mode=mode,
        consumers=[replayer.consumer()],
    )
    run = replayer.run
    changes = [d.as_dict() for d in run.decisions if d.change is not None]
    data = {
        "version": "detection-replay-v1",
        "mode": mode.value,
        "start": begin.isoformat(),
        "end": finish.isoformat(),
        "steps": run.steps,
        "evaluations": len(run.decisions),
        "episodes": len(replayer.store.episodes),
        "recomputations": run.recomputations,
        "data_digest": report.final_digest,
        "decision_digest": run.digest(),
        "changes": changes,
        "note": "each step used only observations available at that time; no look-ahead",
    }
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"detection replay ({mode.value}) {begin:%Y-%m-%d}..{finish:%Y-%m-%d} every {step_hours}h: "
        f"{data['evaluations']} evaluations, {data['episodes']} episode(s), recomputations "
        f"{data['recomputations']}; data digest {data['data_digest']}, decision digest "
        f"{data['decision_digest']}"
    )
    table = Table("as of", "period", "value", "expected", "z", "stage", "change", "event")
    for c in changes[-40:]:
        table.add_row(
            c["as_of"][:16],
            c["period_start"][:10],
            str(c["value"]),
            "-" if c["baseline_expected"] is None else f"{c['baseline_expected']:.1f}",
            str(c["z"]),
            c["stage"] or "-",
            c["change"],
            str(c["event_id"]),
        )
    base.console.print(table)
    base.console.print(f"[dim]{data['note']}[/]")


# --------------------------------------------------------------------------- calibration
@detect_app.command("calibrate")
def detect_calibrate(
    dataset: Annotated[str, typer.Option("--dataset", help="synthetic")] = "synthetic",
    provider: Annotated[str, typer.Option("--provider")] = "synthetic_trends",
    series: Annotated[int, typer.Option(min=50, max=5000)] = 300,
    as_json: base.JsonOpt = False,
) -> None:
    """Calibrate null distributions on a TRAINING split and store them (synthetic only here)."""
    from dataclasses import replace

    from trend_agent.detection.calibration import calibrate
    from trend_agent.detection.evaluation import build_world
    from trend_agent.detection.synthetic import benchmark_specs

    if dataset != "synthetic":
        raise base._fail(
            "only --dataset synthetic is available: calibrating on real data needs stored, "
            "in-control real history (none in this environment)",
            2,
        )
    container, svc = _service()
    specs = [
        replace(s, provider=provider) for s in benchmark_specs("train", series) if not s.events
    ]
    cal = calibrate(
        build_world(specs).points,
        svc.loaded,
        version=f"synthetic-{svc.cfg.threshold_version}",
        source="synthetic",
        dataset=f"synthetic-train-null-{len(specs)}x150d",
        now=svc.now(),
    )
    with transaction(svc.session_factory) as session:
        runs = DetectionRunRepository(session)
        for key, params in cal.strata.items():
            runs.save_calibration(
                version=cal.version,
                stratum=key,
                source="synthetic",
                dataset=cal.dataset,
                method="empirical_tail+gpd",
                parameters=params,
                train_start=cal.train_start,
                train_end=cal.train_end,
            )
    _close(container)
    data = cal.summary()
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"calibration {cal.version} on {cal.dataset}: {cal.periods} null series-periods; "
        f"stored {len(cal.strata)} strata (applies to SYNTHETIC series only)"
    )
    table = Table("k", "h", "ARL0 empirical", "ARL0 normal theory")
    for row in cal.cusum_table:
        if row["k"] == svc.cfg.cusum.k:
            table.add_row(
                str(row["k"]),
                str(row["h"]),
                str(row["arl0_empirical"] or f">{row['arl0_lower_bound']}"),
                str(row["arl0_siegmund_normal"]),
            )
    base.console.print(table)
    base.console.print(
        f"smallest h with ARL0 >= {svc.cfg.cusum.target_arl0}: {cal.recommended_for_k}"
    )


@detect_app.command("evaluate")
def detect_evaluate(
    dataset: Annotated[str, typer.Option("--dataset", help="synthetic")] = "synthetic",
    test_series: Annotated[int, typer.Option(min=50, max=5000)] = 400,
    null_series: Annotated[int, typer.Option(min=50, max=10000)] = 1000,
    save: Annotated[bool, typer.Option("--save/--no-save")] = True,
    json_out: Annotated[Path | None, typer.Option("--json-out")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Evaluate against independent SYNTHETIC ground truth (calibrated on a disjoint split)."""
    from trend_agent.detection.evaluation import run_synthetic_benchmark

    if dataset != "synthetic":
        raise base._fail(
            "only --dataset synthetic exists: no independently labelled real surges are "
            "available, so real-world precision and recall remain unknown",
            2,
        )
    container, svc = _service()
    started = svc.now()
    result, _cal = run_synthetic_benchmark(
        svc.loaded, test_count=test_series, null_count=null_series, now=started
    )
    data = result.as_dict()
    if save:
        with transaction(svc.session_factory) as session:
            runs = DetectionRunRepository(session)
            for name, m in (
                ("calibrated", result.test),
                ("uncalibrated", result.test_uncalibrated),
                ("null_scale", result.null_scale),
            ):
                runs.save_evaluation(
                    {
                        "evaluation_dataset": m.dataset,
                        "dataset_split": f"{m.split}:{name}",
                        "detector_version": svc.cfg.algorithm_version,
                        "configuration_version": svc.loaded.version_tag,
                        "started_at": started,
                        "completed_at": svc.now(),
                        "precision": m.precision,
                        "recall": m.recall,
                        "false_alert_rate": m.false_alert_rate_per_series_day,
                        "false_alerts_per_day": m.false_alerts_per_day,
                        "detection_delay": m.delay_median_days,
                        "detection_delay_p90": m.delay_p90_days,
                        "duplication_rate": m.duplication_rate,
                        "coverage": m.coverage,
                        "severity_spearman": m.severity_spearman,
                        "is_synthetic": True,
                        "evaluation_metadata": {
                            **m.as_dict(),
                            "label": "SYNTHETIC - not real-world performance",
                            "calibration": data["calibration"]["version"],
                        },
                    }
                )
    _close(container)
    if json_out is not None:
        json_out.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        "[bold yellow]SYNTHETIC EVALUATION[/] - independent generator labels; NOT real-world "
        "performance (real-world precision/recall remain unknown)."
    )
    table = Table(
        "set",
        "precision",
        "recall",
        "FA/day",
        "FA/1000 series/day",
        "delay (d)",
        "dup",
        "confirmed P/R",
    )
    for name, m in (
        ("test (calibrated)", result.test),
        ("test (no calibration)", result.test_uncalibrated),
        ("null scale", result.null_scale),
    ):
        table.add_row(
            name,
            str(m.precision),
            str(m.recall),
            str(m.false_alerts_per_day),
            str(m.false_alerts_per_1000_series_day),
            f"{m.delay_median_days} (p90 {m.delay_p90_days})",
            str(m.duplication_rate),
            f"{m.confirmed_precision}/{m.confirmed_recall}",
        )
    base.console.print(table)
    base.console.print(f"recall by kind: {result.test.recall_by_kind}")
    base.console.print(
        f"severity vs true magnitude (Spearman): {result.test.severity_spearman} "
        f"(p={result.test.severity_spearman_p}); coverage {result.test.coverage}"
    )
    base.console.print(f"[dim]sizes {result.sizes}; seconds {result.seconds}[/]")


@detect_app.command("demo")
def detect_demo(
    reset: Annotated[
        bool, typer.Option("--reset/--no-reset", help="Remove earlier Phase 4 demo data first.")
    ] = True,
    null_series: Annotated[int, typer.Option(min=50, max=10000)] = 1000,
    as_json: base.JsonOpt = False,
) -> None:
    """End-to-end SYNTHETIC demonstration (scenarios A-N) with self-checks."""
    from trend_agent.detection.demo import run_demo

    container = base._container(require_db=True)
    try:
        container.detection_config()
        container.monitoring_config()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"configuration: {exc}", 2) from None
    try:
        report = run_demo(container, reset_first=reset, null_series=null_series)
    finally:
        _close(container)
    data = report.as_dict()
    if as_json:
        base._emit_json(data)
    else:
        base.console.print(
            "[bold yellow]SYNTHETIC DETECTION DEMO - fictional subjects, simulated clock; "
            "not real search interest.[/]"
        )
        table = Table(
            "sc", "query", "cat", "class", "status", "confidence", "max sev", "first triggers"
        )
        for e in data["episodes"]:
            table.add_row(
                e["scenario"],
                escape(e["query"]),
                e["category"],
                e["classification"],
                e["status"],
                e["confidence"],
                f"{e['max_severity']:.0f}",
                ",".join(e["first_triggers"]) or "-",
            )
        base.console.print(table)
        checks = Table("scenario", "check", "result", "detail", title="Scenario checks")
        for c in report.checks:
            checks.add_row(
                c.scenario,
                escape(c.name),
                "[green]PASS[/]" if c.passed else "[red]FAIL[/]",
                escape(c.detail[:140]),
            )
        base.console.print(checks)
        base.console.print(f"[dim]{report.runs} detection runs in {data['seconds']}s. {NOTE}[/]")
    if not report.passed:
        raise typer.Exit(1)
