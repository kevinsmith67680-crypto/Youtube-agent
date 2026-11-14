"""``trend-agent system ...``: operating mode, readiness, providers, costs, health, the
production scheduler and worker processes, smoke tests, backups, benchmarks and the demo."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError

system_app = typer.Typer(help="Phase 8 operations: modes, readiness, health, costs, processes.")


def _ops_cfg(container: Any) -> Any:
    from trend_agent.ops.config import load_operations_config

    try:
        return load_operations_config(container.settings.operations_config_dir)
    except (ValueError, OSError) as exc:
        raise base._fail(f"operations configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


def _snapshot(container: Any, *, with_discovery: bool = True) -> Any:
    from trend_agent.ops.system import SystemService

    return SystemService(container).snapshot(with_discovery=with_discovery)


def _enforce(container: Any) -> None:
    from trend_agent.ops.production import ProductionConfigError, enforce

    try:
        for w in enforce(container.settings):
            base.err_console.print(f"[yellow]config warning[/] {w.code}: {escape(w.detail)}")
    except ProductionConfigError as exc:
        raise base._fail(str(exc), 2) from None


def _mode_lines(d: dict[str, Any]) -> None:
    style = {"DEGRADED": "red", "DEMONSTRATION": "magenta"}.get(d["mode"], "cyan")
    base.console.print(
        f"[bold]operating mode[/] [{style}]{d['mode']}[/] (base {d['base_mode']}); direct "
        f"YouTube Search surge claims: "
        f"{'ENABLED' if d['direct_search_claims_enabled'] else 'DISABLED'}"
    )
    for r in d["reasons"]:
        base.console.print(f"  - {escape(r)}")
    if d["next_mode_requirements"]:
        base.console.print("[bold]to reach a more capable mode[/]")
        for r in d["next_mode_requirements"]:
            base.console.print(f"  - {escape(r)}")


# ---------------------------------------------------------------------- providers
@system_app.command("providers")
def providers(
    no_discovery: Annotated[bool, typer.Option("--no-discovery")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Provider capability readiness, signal classes and the operating mode they allow."""
    container = base._container(require_db=True)
    snap = _snapshot(container, with_discovery=not no_discovery)
    _close(container)
    data = snap.as_dict()
    if as_json:
        base._emit_json(
            {k: data[k] for k in ("operating_mode", "providers", "validation", "notes")}
        )
        return
    table = Table("provider", "signal classes", "readiness", "credentials", "last live check")
    for p in data["providers"]:
        chk = p["last_check"]
        table.add_row(
            p["name"],
            ", ".join(p["signal_classes"]) or "-",
            p["level"],
            p["credentials"],
            "never" if chk is None else f"{chk['checked_at'][:16]} {'ok' if chk['ok'] else 'FAIL'}",
        )
    base.console.print(table)
    _mode_lines(data["operating_mode"])
    for n in data["notes"]:
        base.console.print(f"[yellow]note[/] {escape(n)}")


# ---------------------------------------------------------------------- health
@system_app.command("health")
def health_cmd(as_json: base.JsonOpt = False) -> None:
    """Liveness, database, workers/scheduler and provider health (exit 1 when failing)."""
    from trend_agent.ops import health

    container = base._container(require_db=False)
    results = [health.live(), health.database(container.engine)]
    if results[1].ok:
        results.append(health.workers(container.session_factory, container.clock()))
        snap = _snapshot(container)
        usable = [r.name for r in snap.readiness if r.usable_for_real_data]
        results.append(
            health.CheckResult(
                "providers",
                health.OK if usable else health.DEGRADED,
                f"usable real providers: {', '.join(usable) or 'none'}; mode "
                f"{snap.decision.mode.value}",
                {"operating_mode": snap.decision.mode.value},
            )
        )
    _close(container)
    if as_json:
        base._emit_json([r.as_dict() for r in results])
    else:
        for r in results:
            colour = {"ok": "green", "degraded": "yellow"}.get(r.status, "red")
            base.console.print(f"[{colour}]{r.status:>8}[/] {r.name}: {escape(r.detail)}")
    if any(r.status == health.FAIL for r in results):
        raise typer.Exit(1)


# ---------------------------------------------------------------------- costs
@system_app.command("costs")
def costs(as_json: base.JsonOpt = False) -> None:
    """Measured spend against DAILY_API_BUDGET, DAILY/MONTHLY_LLM_BUDGET and the limits."""
    container = base._container(require_db=True)
    rep = container.budget_guard().report(fresh=True)
    _close(container)
    data = rep.as_dict()
    if as_json:
        base._emit_json(data)
        return
    table = Table("budget", "used", "limit", "unit", "state", "window")
    for b in data["budgets"]:
        table.add_row(
            b["name"], f"{b['used']:.4f}", f"{b['limit']:.2f}", b["unit"], b["state"], b["window"]
        )
    base.console.print(table)
    base.console.print(f"LLM today by stage (USD): {data['llm_breakdown_today_usd']}")
    base.console.print(f"API units by provider: {data['api_breakdown_today_units'] or '{}'}")
    base.console.print(f"limits: {data['limits']}")
    for n in data["notes"]:
        base.console.print(f"[dim]{escape(n)}[/]")


# ---------------------------------------------------------------------- readiness
@system_app.command("readiness")
def readiness(
    as_json: base.JsonOpt = False,
    write: Annotated[Path | None, typer.Option("--write", help="Also write JSON here.")] = None,
) -> None:
    """Production readiness scorecard (PASS / FAIL / BLOCKED / NOT_APPLICABLE)."""
    from trend_agent.ops.readiness import build_scorecard

    container = base._container(require_db=True)
    snap = _snapshot(container)
    card = build_scorecard(container, snap)
    _close(container)
    data = card.as_dict() | {"operating_mode": snap.decision.as_dict()}
    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(json.dumps(data, indent=2, default=str) + "\n")
    if as_json:
        base._emit_json(data)
        return
    table = Table("item", "area", "scope", "status", "evidence")
    colour = {"PASS": "green", "FAIL": "red", "BLOCKED": "yellow", "NOT_APPLICABLE": "dim"}
    for i in data["items"]:
        table.add_row(
            i["key"],
            i["area"],
            i["scope"],
            f"[{colour[i['status']]}]{i['status']}[/]",
            escape(i["evidence"][:110]),
        )
    base.console.print(table)
    base.console.print(f"[bold]release state[/] {data['release_state']}  counts {data['counts']}")
    for r in data["reasons"]:
        base.console.print(f"  - {escape(r)}")
    _mode_lines(data["operating_mode"])


# ---------------------------------------------------------------------- audit
@system_app.command("audit")
def audit(
    as_json: base.JsonOpt = False,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 on any error.")] = False,
) -> None:
    """Configuration and deployment self-audit, evaluated as if this were production."""
    from trend_agent.ops import health, production

    container = base._container(require_db=False)
    s = container.settings
    findings: list[dict[str, str]] = [
        {"severity": p.severity, "code": p.code, "detail": p.detail} for p in production.check(s)
    ]
    db = health.database(container.engine)
    if not db.ok:
        findings.append({"severity": "error", "code": "database", "detail": db.detail})
    try:
        tracked = subprocess.run(
            ["git", "ls-files", ".env", "*.pem", "*.key"],  # noqa: S607 - git from PATH
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        tracked = []
    if tracked:
        findings.append(
            {
                "severity": "error",
                "code": "secret_files_tracked",
                "detail": f"tracked by git: {', '.join(tracked)}",
            }
        )
    if s.environment.lower() not in ("production", "prod"):
        findings.append(
            {
                "severity": "info",
                "code": "environment",
                "detail": f"environment={s.environment}: production checks above are "
                "advisory here and enforced only with TREND_AGENT_ENVIRONMENT=production",
            }
        )
    if db.ok:
        snap = _snapshot(container)
        for r in snap.readiness:
            if r.synthetic or r.level.value in ("DISABLED",):
                continue
            if r.level.value in ("NOT_CONFIGURED", "ACCESS_RESTRICTED"):
                findings.append(
                    {
                        "severity": "info",
                        "code": f"provider:{r.name}",
                        "detail": f"{r.level.value}: {r.detail[:160]}",
                    }
                )
        findings.append(
            {
                "severity": "info",
                "code": "operating_mode",
                "detail": f"{snap.decision.mode.value} (base {snap.decision.base_mode.value})",
            }
        )
    _close(container)
    if as_json:
        base._emit_json(findings)
    else:
        colour = {"error": "red", "warning": "yellow", "info": "cyan"}
        for f in findings:
            base.console.print(
                f"[{colour[f['severity']]}]{f['severity']:>7}[/] {f['code']}: {escape(f['detail'])}"
            )
        base.console.print(
            "[dim]Static scans (pip-audit, npm audit, bandit, detect-secrets) run in CI; see "
            "docs/security_audit.md.[/]"
        )
    if strict and any(f["severity"] == "error" for f in findings):
        raise typer.Exit(1)


# ---------------------------------------------------------------------- smoke test
@system_app.command("smoke-test")
def smoke_test(
    provider: Annotated[list[str] | None, typer.Option("--provider", "-p")] = None,
    region: Annotated[str, typer.Option("--region")] = "US",
    as_json: base.JsonOpt = False,
) -> None:
    """Live provider check: at most one minimal request per AVAILABLE provider (YouTube:
    videoCategories.list = 1 quota unit). Providers that are not configured are skipped."""
    from trend_agent.db.repositories.ops import ProviderCheckRepository
    from trend_agent.db.session import transaction
    from trend_agent.ops.providers import smoke_test as run_smoke

    container = base._container(require_db=True)
    try:
        container.discovery()
        sources = container.discovery_config().sources
    except ConfigError:
        sources = ()
    wanted = set(provider or [])

    async def go() -> list[Any]:
        out = []
        for p in container.registry.all():
            if wanted and p.descriptor.name not in wanted:
                continue
            out.append(await run_smoke(p, region=region, discovery_sources=sources))
        return out

    results = base._run(container, go)
    with transaction(container.session_factory) as s:
        repo = ProviderCheckRepository(s)
        for r in results:
            if r.attempted:
                repo.add(
                    provider_name=r.provider,
                    now=container.clock(),
                    ok=r.ok,
                    status=r.status,
                    detail=r.detail,
                    latency_ms=r.latency_ms,
                    units_spent=r.units_spent,
                    signal_classes=r.signal_classes,
                )
    if as_json:
        base._emit_json([r.as_dict() for r in results])
        return
    table = Table("provider", "attempted", "result", "latency ms", "units", "detail")
    for r in results:
        table.add_row(
            r.provider,
            "yes" if r.attempted else "no",
            "ok" if r.ok else r.status,
            str(r.latency_ms or "-"),
            str(r.units_spent),
            escape(r.detail[:90]),
        )
    base.console.print(table)


# ---------------------------------------------------------------------- processes
@system_app.command("scheduler")
def scheduler(
    synthetic: Annotated[
        bool, typer.Option("--synthetic", help="Run the stages on synthetic data (demo/test).")
    ] = False,
    once: Annotated[bool, typer.Option("--once", help="One tick, then exit.")] = False,
    max_ticks: Annotated[int | None, typer.Option(hidden=True)] = None,
    owner: Annotated[str | None, typer.Option("--id")] = None,
) -> None:
    """The pipeline scheduler: holds the single-owner lease and runs the due stages."""
    from trend_agent.ops.pipeline import PipelineScheduler

    container = base._container(require_db=True)
    _enforce(container)
    sched = PipelineScheduler(container, _ops_cfg(container), synthetic=synthetic, owner=owner)
    base.console.print(f"scheduler {sched.owner} started (lease 'pipeline', synthetic={synthetic})")

    async def main() -> int:
        if once:
            rep = await sched.tick()
            sched.release()
            sched.heartbeat.stop()
            base.console.print(f"leader={rep.leader} mode={rep.mode} ran={rep.ran}")
            return 1
        return await sched.run(max_ticks=max_ticks)

    base._run(container, main)


@system_app.command("worker")
def worker(
    synthetic: Annotated[bool, typer.Option("--synthetic")] = False,
    once: Annotated[bool, typer.Option("--once")] = False,
    max_ticks: Annotated[int | None, typer.Option(hidden=True)] = None,
    worker_id: Annotated[str | None, typer.Option("--id")] = None,
) -> None:
    """A worker: collection jobs + the detection queue, with heartbeats. Scale out freely
    up to TREND_AGENT_MAX_CONCURRENT_WORKERS."""
    from trend_agent.ops.pipeline import PipelineWorker, WorkerLimitError

    container = base._container(require_db=True)
    _enforce(container)
    try:
        w = PipelineWorker(container, _ops_cfg(container), worker_id=worker_id, synthetic=synthetic)
    except ConfigError as exc:
        raise base._fail(f"configuration: {exc}", 2) from None

    async def main() -> int:
        if once:
            w.check_capacity()
            out = await w.tick()
            w.heartbeat.stop()
            base.console.print(f"{w.worker_id}: {out}")
            return 1
        return await w.run(max_ticks=max_ticks)

    try:
        base._run(container, main)
    except WorkerLimitError as exc:
        raise base._fail(str(exc), 3) from None


# ---------------------------------------------------------------------- alerts
@system_app.command("alerts")
def alerts(
    as_json: base.JsonOpt = False, all_: Annotated[bool, typer.Option("--all")] = False
) -> None:
    """Operational alerts (open by default)."""
    from trend_agent.db.repositories.ops import OpsAlertRepository
    from trend_agent.db.session import transaction

    container = base._container(require_db=True)
    with transaction(container.session_factory) as s:
        repo = OpsAlertRepository(s)
        rows = repo.recent() if all_ else repo.open()
        data: list[dict[str, Any]] = [
            {
                "fingerprint": r.fingerprint,
                "severity": r.severity,
                "status": r.status,
                "title": r.title,
                "occurrences": r.occurrences,
                "first_seen": r.first_seen_at.isoformat(),
                "last_seen": r.last_seen_at.isoformat(),
                "notifications_sent": r.notifications_sent,
                "detail": r.detail,
            }
            for r in rows
        ]
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    if not data:
        base.console.print("no open operational alerts")
    for a in data:
        base.console.print(
            f"[{a['severity']}] {a['status']} x{a['occurrences']} "
            f"{escape(a['title'])}: {escape(a['detail'][:160])}"
        )


@system_app.command("dead-letters")
def dead_letters(
    resolve: Annotated[int | None, typer.Option("--resolve", help="Dead letter ID.")] = None,
    note: Annotated[str, typer.Option("--note")] = "resolved by operator",
    as_json: base.JsonOpt = False,
) -> None:
    """Unresolved dead letters (permanently failed work and malformed records)."""
    from trend_agent.db.repositories.ops import DeadLetterRepository
    from trend_agent.db.session import transaction

    container = base._container(require_db=True)
    with transaction(container.session_factory) as s:
        repo = DeadLetterRepository(s)
        if resolve is not None and not repo.resolve(resolve, note, container.clock()):
            raise base._fail(f"no unresolved dead letter {resolve}")
        data: list[dict[str, Any]] = [
            {
                "id": r.id,
                "source": r.source,
                "reference": r.reference,
                "error_type": r.error_type,
                "attempts": r.attempts,
                "last_failed_at": r.last_failed_at.isoformat(),
                "error": r.error,
            }
            for r in repo.open()
        ]
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    for d in data:
        base.console.print(
            f"#{d['id']} {d['source']}:{escape(d['reference'])} x{d['attempts']} "
            f"{d['error_type']}: {escape(d['error'][:140])}"
        )
    if not data:
        base.console.print("no unresolved dead letters")


# ---------------------------------------------------------------------- backups
@system_app.command("backup")
def backup_cmd(
    out: Annotated[Path, typer.Option("--out", help="Directory for the dump.")] = Path("backups"),
    as_json: base.JsonOpt = False,
) -> None:
    """pg_dump (custom format) + SHA-256 manifest with per-table row counts."""
    from trend_agent.ops.backup import BackupError, backup

    container = base._container(require_db=True)
    url = container.settings.database_url.get_secret_value()
    _close(container)
    try:
        res = backup(url, out)
    except BackupError as exc:
        raise base._fail(str(exc)) from None
    if as_json:
        base._emit_json(res.as_dict())
    else:
        base.console.print(
            f"backup {res.path} ({res.bytes} bytes, sha256 {res.sha256[:16]}..., "
            f"{res.seconds}s, schema {res.schema_revision})"
        )


@system_app.command("restore-drill")
def restore_drill_cmd(
    backup_file: Annotated[Path, typer.Argument(help="A .dump written by `system backup`.")],
    keep: Annotated[bool, typer.Option("--keep", help="Keep the scratch database.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Restore a backup into <db>_restore_drill, verify schema and row counts, record it."""
    from trend_agent.db.repositories.ops import SystemStateRepository
    from trend_agent.db.session import transaction
    from trend_agent.ops.backup import BackupError, restore_drill
    from trend_agent.ops.readiness import RESTORE_DRILL_KEY

    container = base._container(require_db=True)
    url = container.settings.database_url.get_secret_value()
    try:
        res = restore_drill(url, backup_file, keep=keep)
    except BackupError as exc:
        with transaction(container.session_factory) as s:
            SystemStateRepository(s).put(
                RESTORE_DRILL_KEY,
                {"ok": False, "at": container.clock().isoformat(), "error": str(exc)[:300]},
                container.clock(),
            )
        _close(container)
        raise base._fail(f"restore drill failed: {exc}") from None
    with transaction(container.session_factory) as s:
        SystemStateRepository(s).put(RESTORE_DRILL_KEY, res.as_dict(), container.clock())
    _close(container)
    if as_json:
        base._emit_json(res.as_dict())
    else:
        base.console.print(
            f"restore drill {'PASSED' if res.ok else 'FAILED'}: {res.tables_compared} tables, "
            f"{len(res.mismatches)} mismatches, restore {res.restore_seconds}s, schema "
            f"{res.schema_revision} (scratch database {res.scratch_database} "
            f"{'kept' if keep else 'dropped'})"
        )
        for t, m in res.mismatches.items():
            base.console.print(f"  {t}: expected {m['expected']} restored {m['restored']}")
    if not res.ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------- benchmark
def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)


@system_app.command("benchmark")
def benchmark(
    iterations: Annotated[int, typer.Option("--iterations", "-n", min=5, max=2000)] = 50,
    out: Annotated[Path | None, typer.Option("--out")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Measure health checks, the mode decision and API latency against this database
    (read-only requests through the in-process ASGI client; conditions are recorded)."""
    from fastapi.testclient import TestClient

    from trend_agent.api.app import create_app
    from trend_agent.ops import health

    container = base._container(require_db=True)
    settings = container.settings
    app = create_app(container.dashboard_backend(), settings, ops=container)
    token = settings.api_token.get_secret_value() if settings.api_token else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = TestClient(app)
    results: dict[str, Any] = {}

    def measure(name: str, fn: Any) -> None:
        fn()  # warm-up
        times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            fn()
            times.append((time.perf_counter() - t0) * 1000)
        results[name] = {
            "p50_ms": _pct(times, 0.5),
            "p95_ms": _pct(times, 0.95),
            "max_ms": round(max(times), 2),
            "n": iterations,
        }

    measure("health.database", lambda: health.database(container.engine))
    measure("health.workers", lambda: health.workers(container.session_factory, container.clock()))
    measure("system.snapshot", lambda: _snapshot(container))
    for path in (
        "/health/live",
        "/health/ready",
        "/api/system/mode",
        "/api/overview",
        "/api/opportunities",
        "/api/recommendations",
        "/metrics",
    ):

        def call(p: str = path) -> None:
            r = client.get(p, headers=headers)
            if r.status_code >= 500:
                raise RuntimeError(f"{p}: {r.status_code}")

        measure(f"GET {path}", call)
    with container.engine.connect() as conn:
        import sqlalchemy as sa

        sizes = {
            t: int(conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
            for t in (
                "ts_observations",
                "content_opportunities",
                "video_recommendations",
                "surge_events",
                "forecast_predictions",
            )
        }
    _close(container)
    report = {
        "measured_at": container.clock().isoformat(),
        "conditions": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "iterations": iterations,
            "client": "in-process ASGI",
            "database_rows": sizes,
            "api_token_configured": bool(token),
        },
        "results": results,
        "notes": [
            "Single client, sequential requests; not a concurrency test (see scripts/load_test.py)."
        ],
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
    if as_json:
        base._emit_json(report)
        return
    table = Table("measurement", "p50 ms", "p95 ms", "max ms")
    for k, v in results.items():
        table.add_row(k, str(v["p50_ms"]), str(v["p95_ms"]), str(v["max_ms"]))
    base.console.print(table)
    base.console.print(f"conditions: {report['conditions']}")


# ---------------------------------------------------------------------- demo
@system_app.command("demo")
def demo(as_json: base.JsonOpt = False) -> None:
    """The DEMONSTRATION mode: the isolated synthetic end-to-end world (Phases 3-7 run in
    memory; nothing is read from or written to the database), labelled synthetic."""
    from datetime import UTC, datetime

    from trend_agent.cli_recommend import demo_cache_path
    from trend_agent.discovery.config import resolve_config_dir
    from trend_agent.ops.modes import decide_mode
    from trend_agent.recommendations.demo import demo_backend

    settings = base.SETTINGS_FACTORY()
    t0 = time.perf_counter()
    be = demo_backend(resolve_config_dir(settings.discovery_config_dir), demo_cache_path())
    ov = be.overview()
    recs = be.recommendations(page=1, page_size=100)
    decision = decide_mode([], now=datetime.now(UTC), demo=True)
    data: dict[str, Any] = {
        "operating_mode": decision.as_dict(),
        "synthetic": ov.synthetic,
        "seconds": round(time.perf_counter() - t0, 2),
        "monitored_queries": ov.monitored_queries,
        "active_surges": ov.active_surges,
        "opportunities": ov.opportunities,
        "approved_recommendations": ov.approved_recommendations,
        "research_candidates": ov.research_candidates,
        "recommendations": recs.total,
        "notes": ov.notes,
    }
    if as_json:
        base._emit_json(data)
        return
    _mode_lines(data["operating_mode"])
    base.console.print(
        f"[magenta]SYNTHETIC DEMO[/] queries {data['monitored_queries']}, surges "
        f"{data['active_surges']}, opportunities {data['opportunities']}, recommendations "
        f"{data['recommendations']} (approved {data['approved_recommendations']}, research "
        f"{data['research_candidates']}) in {data['seconds']}s"
    )
    base.console.print("Serve it with: trend-agent dashboard serve --demo --ui")
