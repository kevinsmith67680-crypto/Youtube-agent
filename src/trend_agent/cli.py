"""Command-line interface (``trend-agent``).

Human-readable tables go to stdout; ``--json`` switches a command to machine-readable
JSON on stdout. Structured logs go to stderr (``-v`` for INFO, default WARNING).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from trend_agent import __version__
from trend_agent.comparability import group_by_comparability
from trend_agent.config import Settings, get_settings
from trend_agent.container import AppContainer, build_container
from trend_agent.db.repositories import (
    IngestionRunRepository,
    ObservationRepository,
    QueryRepository,
)
from trend_agent.db.session import check_connection, transaction
from trend_agent.ingestion.service import IngestionSummary, SearchInterestIngestion
from trend_agent.logging import configure_logging
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import ProviderError, ProviderNotConfiguredError
from trend_agent.providers.google_trends import PROVIDER_NAME as TRENDS
from trend_agent.providers.google_trends import PUBLICLY_ANNOUNCED_FEATURES
from trend_agent.providers.mock import MockScenario
from trend_agent.providers.quota import InMemoryQuotaStore
from trend_agent.schemas.enums import IngestionStatus, SearchProperty, TimeGranularity
from trend_agent.schemas.provider import (
    PopularVideosRequest,
    ProviderDescriptor,
    VideoSearchOrder,
    VideoSearchRequest,
)
from trend_agent.utils.codes import CodeValidationError
from trend_agent.utils.text import QueryValidationError, canonicalize_query

app = typer.Typer(
    name="trend-agent",
    help="YouTube Search Demand Intelligence Agent: data acquisition, discovery, monitoring.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
providers_app = typer.Typer(help="Inspect registered data providers.", no_args_is_help=True)
youtube_app = typer.Typer(help="YouTube Data API v3 operations.", no_args_is_help=True)
trends_app = typer.Typer(help="Google Trends search-interest access.", no_args_is_help=True)
data_app = typer.Typer(help="Inspect stored data, runs and retention.", no_args_is_help=True)
quota_app = typer.Typer(help="Local quota accounting.", no_args_is_help=True)
db_app = typer.Typer(help="Database schema management.", no_args_is_help=True)
for sub, name in [
    (providers_app, "providers"),
    (youtube_app, "youtube"),
    (trends_app, "trends"),
    (data_app, "data"),
    (quota_app, "quota"),
    (db_app, "db"),
]:
    app.add_typer(sub, name=name)

# emoji=False: source keys such as "youtube:chart:US:20" must print verbatim (Rich would
# otherwise turn ":chart:" into an emoji).
console = Console(emoji=False)
err_console = Console(stderr=True, emoji=False)

# Tests replace this to inject fakes (HTTP transport, clock, database).
ContainerFactory = Callable[..., AppContainer]
CONTAINER_FACTORY: ContainerFactory = build_container
SETTINGS_FACTORY: Callable[[], Settings] = get_settings

JsonOpt = Annotated[bool, typer.Option("--json", help="Emit JSON on stdout.")]


# ------------------------------------------------------------------------- helpers
def _emit_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _fail(message: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[bold red]Error:[/] {escape(message)}")
    return typer.Exit(code)


def _invalid(exc: Exception) -> typer.Exit:
    """Turn request-validation errors into a one-line message."""
    if isinstance(exc, ValidationError):
        messages = [str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors()]
        return _fail("; ".join(messages))
    return _fail(str(exc))


def _provider_error(exc: ProviderError) -> typer.Exit:
    hint = ""
    if isinstance(exc, ProviderNotConfiguredError) and exc.missing:
        hint = (
            f"\nMissing configuration: {', '.join(exc.missing)}. "
            "Set it in your environment or .env (see .env.example)."
        )
    return _fail(f"[{exc.error_type}] {exc.message}{hint}", 2 if hint else 1)


def _container(*, require_db: bool) -> AppContainer:
    """Build the container. Without a database, read-only provider commands still work
    using an in-memory quota estimate (with a warning)."""
    settings = SETTINGS_FACTORY()
    container = CONTAINER_FACTORY(settings)
    ok, detail = check_connection(container.engine)
    if ok:
        return container
    if require_db:
        container.engine.dispose()
        raise _fail(
            f"database unavailable ({detail}).\nStart it with `docker compose up -d` and run "
            "`trend-agent db upgrade`; check TREND_AGENT_DATABASE_URL.",
            3,
        )
    container.engine.dispose()
    err_console.print(
        "[yellow]Warning:[/] database unavailable; quota usage for this command is tracked "
        "in memory only and will not be persisted."
    )
    return CONTAINER_FACTORY(settings, quota_store=InMemoryQuotaStore())


def _run[T](container: AppContainer, factory: Callable[[], Awaitable[T]]) -> T:
    """Run an async operation; SIGTERM/SIGINT cancel it gracefully."""

    async def main() -> T:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if task is not None:
            # Not supported on Windows event loops; SIGINT still works there.
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await factory()
        finally:
            await container.aclose()

    try:
        return asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise _fail("interrupted; any in-flight ingestion run was marked cancelled", 130) from None


def _parse_when(value: str | None, label: str) -> datetime | None:
    """Parse YYYY-MM-DD (as 00:00 UTC) or an RFC 3339 timestamp with an explicit offset."""
    if value is None:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"{label}: expected YYYY-MM-DD or RFC 3339, got {value!r}") from None
    if parsed.tzinfo is None:
        raise _fail(f"{label}: timestamp needs an explicit UTC offset (e.g. 'Z')")
    return parsed.astimezone(UTC)


def _fmt(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if dt else "-"


def _print_summary(summary: IngestionSummary) -> None:
    colour = {
        IngestionStatus.SUCCEEDED: "green",
        IngestionStatus.PARTIAL: "yellow",
        IngestionStatus.FAILED: "red",
        IngestionStatus.CANCELLED: "red",
    }.get(summary.status, "white")
    console.print(
        f"Run [bold]{summary.run_id or '-'}[/]  provider={summary.provider}  "
        f"operation={summary.operation}  status=[{colour}]{summary.status.value}[/]"
    )
    table = Table(
        "record kind",
        "received",
        "stored",
        "already present",
        "dup in batch",
        "rejected",
        "withheld (policy)",
    )
    for kind, counts in sorted(summary.by_kind.items()):
        table.add_row(
            kind,
            *(
                str(counts.get(k, 0))
                for k in (
                    "received",
                    "stored",
                    "already_present",
                    "duplicate_in_batch",
                    "rejected",
                    "withheld",
                )
            ),
        )
    if summary.by_kind:
        console.print(table)
    if summary.rejected_by_reason:
        console.print(
            "Rejected by reason: "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary.rejected_by_reason.items()))
        )
    failed = [f for f in summary.fetches if not f.succeeded]
    for f in failed:
        state = "not attempted" if not f.attempted else "failed"
        console.print(
            f"[yellow]{escape(f'{f.unit}: {state} ({f.error_type}) {f.error_message}')}[/]"
        )
    if summary.quota_used:
        console.print(f"Estimated quota used: {summary.quota_used}")
    if summary.error_type and not failed:
        console.print(f"[red]{escape(f'{summary.error_type}: {summary.error_message}')}[/]")
    for warning in summary.warnings:
        console.print(f"[yellow]warning:[/] {escape(warning)}")


def _descriptor_dict(d: ProviderDescriptor) -> dict[str, Any]:
    return {
        "name": d.name,
        "display_name": d.display_name,
        "version": d.version,
        "type": d.provider_type.value,
        "enabled": d.enabled,
        "capabilities": sorted(c.value for c in d.capabilities),
        "measurement_types": sorted(m.value for m in d.measurement_types),
        "search_properties": sorted(p.value for p in d.search_properties),
        "granularities": sorted(g.value for g in d.supported_granularities),
        "regions": "any ISO 3166-1" if d.supported_regions is None else sorted(d.supported_regions),
        "requires_authentication": d.requires_authentication,
        "historical_data": d.historical_data_available,
        "data_nature": d.data_nature.value,
        "retention": [
            {
                "record_kind": r.record_kind.value,
                "persist_allowed": r.persist_allowed,
                "max_retention_days": r.max_retention.days if r.max_retention else None,
                "refresh_required": r.refresh_required,
            }
            for r in d.retention
        ],
        "usage_restrictions": list(d.usage_restrictions),
    }


# ------------------------------------------------------------------------- root
@app.callback()
def _root(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="INFO-level logs on stderr.")
    ] = False,
) -> None:
    settings = SETTINGS_FACTORY()
    level = (
        "INFO" if verbose else ("WARNING" if settings.log_level == "INFO" else settings.log_level)
    )
    configure_logging(level, settings.log_format.value)


@app.command()
def version() -> None:
    """Print the application version."""
    console.print(__version__)


@app.command()
def doctor(
    live: Annotated[
        bool,
        typer.Option("--live", help="Also validate the YouTube key with one 1-unit API call."),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show configuration, database connectivity and provider readiness (no secrets)."""
    settings = SETTINGS_FACTORY()
    container = CONTAINER_FACTORY(settings)
    db_ok, db_detail = check_connection(container.engine)
    head = _alembic_head()
    schema_current = db_ok and head is not None and f"revision={head}" in db_detail
    if not db_ok:
        container.engine.dispose()
        container = CONTAINER_FACTORY(settings, quota_store=InMemoryQuotaStore())
    reports = container.registry.status_reports()
    live_result: str | None = None
    if live:

        async def check() -> str:
            categories = await container.youtube().video_categories("US", RequestContext())
            return f"ok ({len(categories)} categories returned)"

        try:
            live_result = _run(container, check)
        except typer.Exit:
            raise
        except ProviderError as exc:
            live_result = f"failed: [{exc.error_type}] {exc.message}"
    else:
        _run(container, _noop)

    result = {
        "version": __version__,
        "configuration": settings.describe(),
        "database": {
            "ok": db_ok,
            "detail": db_detail,
            "schema_head": head,
            "schema_current": schema_current,
        },
        "providers": [r.model_dump(mode="json") for r in reports],
        "youtube_live_check": live_result,
    }
    if as_json:
        _emit_json(result)
    else:
        console.print(f"[bold]trend-agent {__version__}[/]")
        cfg = Table("setting", "value", title="Configuration (secrets redacted)")
        for key, value in settings.describe().items():
            cfg.add_row(key, str(value))
        console.print(cfg)
        colour = "green" if db_ok else "red"
        console.print(
            f"Database: [{colour}]{'reachable' if db_ok else 'UNREACHABLE'}[/] {db_detail}"
        )
        if db_ok and not schema_current:
            console.print(
                f"[yellow]Schema is not at head ({head}); run `trend-agent db upgrade`.[/]"
            )
        table = Table("provider", "status", "detail", title="Providers")
        for r in reports:
            table.add_row(r.provider_name, r.status.value, r.detail)
        console.print(table)
        if live_result is not None:
            console.print(f"YouTube live check: {live_result}")
    if not (db_ok and schema_current):
        raise typer.Exit(1)


async def _noop() -> None:
    return None


def _alembic_head() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(_alembic_ini()))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return None


def _alembic_ini() -> Path:
    for base in (Path.cwd(), *Path(__file__).resolve().parents):
        candidate = base / "alembic.ini"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("alembic.ini not found")


# ------------------------------------------------------------------------- providers
@providers_app.command("list")
def providers_list(as_json: JsonOpt = False) -> None:
    """List providers, their capabilities, readiness and declared semantics."""
    container = _container(require_db=False)
    reports = {r.provider_name: r for r in container.registry.status_reports()}
    rows = []
    for provider in container.registry.all():
        d = provider.descriptor
        info = _descriptor_dict(d)
        info["status"] = reports[d.name].status.value
        info["status_detail"] = reports[d.name].detail
        rows.append(info)
    _run(container, _noop)
    if as_json:
        _emit_json(rows)
        return
    table = Table(
        "name",
        "type",
        "status",
        "capabilities",
        "measurement types",
        "granularity",
        "auth",
        "data",
        title="Providers",
    )
    for r in rows:
        table.add_row(
            r["name"],
            r["type"],
            r["status"],
            "\n".join(r["capabilities"]),
            "\n".join(r["measurement_types"]),
            ", ".join(r["granularities"]) or "unknown",
            "yes" if r["requires_authentication"] else "no",
            r["data_nature"],
        )
    console.print(table)
    for r in rows:
        if r["status"] != "available":
            console.print(f"[yellow]{r['name']}[/]: {r['status_detail']}")


# ------------------------------------------------------------------------- youtube
@youtube_app.command("search")
def youtube_search(
    query: Annotated[str, typer.Option("--query", "-q", help="Search query.")],
    region: Annotated[str | None, typer.Option(help="ISO 3166-1 alpha-2 region, e.g. GB.")] = None,
    language: Annotated[str | None, typer.Option(help="Relevance language, e.g. en.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="Max results (paged by 50).")] = 20,
    order: Annotated[VideoSearchOrder, typer.Option(help="Result ordering.")] = (
        VideoSearchOrder.RELEVANCE
    ),
    published_after: Annotated[str | None, typer.Option(help="YYYY-MM-DD or RFC 3339.")] = None,
    published_before: Annotated[str | None, typer.Option(help="YYYY-MM-DD or RFC 3339.")] = None,
    persist: Annotated[bool, typer.Option(help="Store results (subject to retention).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Search videos (search.list: 1 call from the 100/day search bucket per page)."""
    try:
        request = VideoSearchRequest(
            query=query,
            region=region,
            language=language,
            max_results=limit,
            order=order,
            published_after=_parse_when(published_after, "--published-after"),
            published_before=_parse_when(published_before, "--published-before"),
        )
    except (ValueError, CodeValidationError) as exc:
        raise _invalid(exc) from None
    container = _container(require_db=persist)
    if persist:
        summary = _run(
            container,
            lambda: container.ingestion().ingest_video_search(
                container.youtube().descriptor.name, request
            ),
        )
        _emit_json(summary.model_dump(mode="json")) if as_json else _print_summary(summary)
        if summary.status is IngestionStatus.FAILED:
            raise typer.Exit(1)
        return
    try:
        response = _run(
            container, lambda: container.youtube().search_videos(request, RequestContext())
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    if as_json:
        _emit_json(response.model_dump(mode="json"))
        return
    table = Table("#", "video", "title", "channel", "published", title=f"YouTube search: {query!r}")
    for r in response.results:
        table.add_row(
            str(r.rank), r.url, r.title, r.channel_title or r.channel_id, _fmt(r.published_at)
        )
    console.print(table)
    console.print(
        f"{len(response.results)} results, {response.pages_fetched} page(s); "
        f"more available: {response.more_available}. Source: YouTube Data API. "
        "Search results describe video supply, not search volume."
    )
    for w in response.warnings:
        console.print(f"[yellow]warning:[/] {escape(w)}")


@youtube_app.command("popular")
def youtube_popular(
    region: Annotated[str, typer.Option(help="ISO 3166-1 alpha-2 region, e.g. GB.")] = "US",
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
    category: Annotated[
        str | None, typer.Option(help="Video category id (see `categories`).")
    ] = None,
    persist: Annotated[bool, typer.Option(help="Store results (subject to retention).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Most-popular chart (videos.list chart=mostPopular). Popularity, not search volume."""
    try:
        request = PopularVideosRequest(region=region, max_results=limit, category_id=category)
    except (ValueError, CodeValidationError) as exc:
        raise _invalid(exc) from None
    container = _container(require_db=persist)
    if persist:
        summary = _run(
            container,
            lambda: container.ingestion().ingest_popular_videos(
                container.youtube().descriptor.name, request
            ),
        )
        _emit_json(summary.model_dump(mode="json")) if as_json else _print_summary(summary)
        if summary.status is IngestionStatus.FAILED:
            raise typer.Exit(1)
        return
    try:
        response = _run(
            container, lambda: container.youtube().popular_videos(request, RequestContext())
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    if as_json:
        _emit_json(response.model_dump(mode="json"))
        return
    table = Table(
        "#",
        "video",
        "title",
        "channel",
        "duration",
        "views",
        "published",
        title=f"Most popular - {request.region}",
    )
    for v in response.videos:
        views = v.statistics.view_count if v.statistics else None
        table.add_row(
            str(v.rank),
            v.url,
            v.title,
            v.channel_title or v.channel_id,
            f"{v.duration_seconds}s" if v.duration_seconds is not None else "-",
            f"{views:,}" if views is not None else "hidden",
            _fmt(v.published_at),
        )
    console.print(table)
    console.print("Source: YouTube Data API. Chart popularity is not a measure of search demand.")


@youtube_app.command("categories")
def youtube_categories(
    region: Annotated[str, typer.Option(help="ISO 3166-1 alpha-2 region.")] = "US",
    as_json: JsonOpt = False,
) -> None:
    """List video categories for a region (videoCategories.list, 1 unit)."""
    container = _container(require_db=False)
    try:
        cats = _run(
            container, lambda: container.youtube().video_categories(region, RequestContext())
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    except CodeValidationError as exc:
        raise _fail(str(exc)) from None
    if as_json:
        _emit_json([c.model_dump() for c in cats])
        return
    table = Table("id", "title", "assignable", title=f"Video categories - {region.upper()}")
    for c in cats:
        table.add_row(c.category_id, c.title, str(c.assignable))
    console.print(table)


@youtube_app.command("refresh")
def youtube_refresh(
    horizon_days: Annotated[int, typer.Option(min=0, max=30)] = 2,
    limit: Annotated[int, typer.Option(min=1, max=5000)] = 500,
    as_json: JsonOpt = False,
) -> None:
    """Refresh stored video metadata nearing expiry; delete videos no longer available."""
    container = _container(require_db=True)
    summary = _run(
        container,
        lambda: container.ingestion().refresh_expiring_videos(
            container.youtube().descriptor.name, horizon=timedelta(days=horizon_days), limit=limit
        ),
    )
    _emit_json(summary.model_dump(mode="json")) if as_json else _print_summary(summary)
    if not as_json:
        console.print(
            f"Due: {summary.extra.get('due')}, deleted (unavailable): "
            f"{summary.extra.get('deleted_unavailable')}"
        )


# ------------------------------------------------------------------------- trends
@trends_app.command("status")
def trends_status(as_json: JsonOpt = False) -> None:
    """Report Google Trends access status and the granularity actually supported."""
    container = _container(require_db=False)
    provider = container.registry.get(TRENDS)
    report = provider.status()
    d = provider.descriptor
    _run(container, _noop)
    granularities = sorted(g.value for g in d.supported_granularities)
    data: dict[str, Any] = {
        "status": report.status.value,
        "detail": report.detail,
        "missing_configuration": list(report.missing_configuration),
        "supported_granularities": granularities or None,
        "search_properties": sorted(p.value for p in d.search_properties),
        "measurement_types": sorted(m.value for m in d.measurement_types),
        "publicly_announced_unverified": list(PUBLICLY_ANNOUNCED_FEATURES),
    }
    if as_json:
        _emit_json(data)
        return
    console.print(f"Google Trends: [bold]{report.status.value}[/]")
    console.print(report.detail)
    console.print(
        "Supported granularity: "
        + (", ".join(granularities) if granularities else "unknown (no verified transport)")
    )
    console.print(
        "Search property: "
        + ", ".join(data["search_properties"])
        + " (reported as external_search_interest, never as YouTube demand)"
    )
    console.print("\nPublicly announced for the alpha (unverified here):")
    for line in PUBLICLY_ANNOUNCED_FEATURES:
        console.print(f"  - {line}")


# ------------------------------------------------------------------------- ingest
@app.command()
def ingest(
    query: Annotated[list[str], typer.Option("--query", "-q", help="Query (repeatable).")],
    provider: Annotated[str, typer.Option(help="Provider name, e.g. mock.")] = "mock",
    scenario: Annotated[MockScenario | None, typer.Option(help="Mock scenario.")] = None,
    days: Annotated[int, typer.Option(min=1, max=3650, help="Window length ending today.")] = 90,
    start: Annotated[str | None, typer.Option(help="Window start (overrides --days).")] = None,
    end: Annotated[
        str | None, typer.Option(help="Window end, exclusive (default: 00:00 UTC today).")
    ] = None,
    region: Annotated[str | None, typer.Option(help="ISO 3166-1 region; omit for WORLD.")] = None,
    language: Annotated[str | None, typer.Option(help="Query language, e.g. en.")] = None,
    granularity: Annotated[TimeGranularity, typer.Option()] = TimeGranularity.DAY,
    search_property: Annotated[SearchProperty, typer.Option("--property")] = (
        SearchProperty.YOUTUBE_SEARCH
    ),
    as_json: JsonOpt = False,
) -> None:
    """Ingest search-interest observations (e.g. synthetic data from the mock provider)."""
    container = _container(require_db=True)
    # Default end: 00:00 UTC today (complete days only). A wall-clock end would make every
    # run a new request window - and so a new normalisation batch - defeating idempotency.
    today = datetime.combine(container.clock().date(), time.min, tzinfo=UTC)
    end_dt = _parse_when(end, "--end") or today
    start_dt = _parse_when(start, "--start") or (end_dt - timedelta(days=days))
    options = {"scenario": scenario.value} if scenario else {}
    try:
        request = SearchInterestIngestion(
            provider=provider,
            queries=tuple(query),
            region=region,
            language=language,
            start=start_dt,
            end=end_dt,
            granularity=granularity,
            search_property=search_property,
            provider_options=options,
        )
    except (ValueError, CodeValidationError) as exc:
        container.engine.dispose()
        raise _invalid(exc) from None
    summary = _run(container, lambda: container.ingestion().ingest_search_interest(request))
    if as_json:
        _emit_json(summary.model_dump(mode="json") | {"totals": summary.totals()})
    else:
        _print_summary(summary)
        missing = summary.extra.get("missing_configuration")
        if missing:
            console.print(f"Missing configuration: {', '.join(missing)}")
    if summary.status is IngestionStatus.FAILED:
        raise typer.Exit(2 if summary.error_type == "not_configured" else 1)


# ------------------------------------------------------------------------- data
@data_app.command("inspect")
def data_inspect(
    query: Annotated[str, typer.Option("--query", "-q")],
    region: Annotated[str | None, typer.Option()] = None,
    language: Annotated[str | None, typer.Option()] = None,
    provider: Annotated[str | None, typer.Option()] = None,
    exclude_synthetic: Annotated[bool, typer.Option("--exclude-synthetic")] = False,
    limit: Annotated[int, typer.Option(min=1, max=5000, help="Rows to print.")] = 20,
    as_json: JsonOpt = False,
) -> None:
    """Show stored observations, their provenance and comparability groups."""
    try:
        canonical = canonicalize_query(query)
    except QueryValidationError as exc:
        raise _fail(str(exc)) from None
    container = _container(require_db=True)
    with transaction(container.session_factory) as session:
        query_rows = QueryRepository(session).find_all(canonical)
        stored = ObservationRepository(session).list_for_query(
            canonical,
            language=language,
            region=region.upper() if region else None,
            provider_name=provider,
            include_synthetic=not exclude_synthetic,
        )
    _run(container, _noop)
    groups = group_by_comparability(s.observation for s in stored)
    group_info: list[dict[str, Any]] = []
    for key, members in groups.items():
        first = members[0]
        qualities: dict[str, int] = {}
        for m in members:
            qualities[m.sample_quality.value] = qualities.get(m.sample_quality.value, 0) + 1
        group_info.append(
            {
                "comparability_key": key,
                "provider": first.provider_name,
                "measurement_type": first.measurement_type.value,
                "search_property": first.search_property.value,
                "scale_type": first.scale_type.value,
                "normalization_batch": first.normalization_context.batch_id,
                "granularity": first.granularity.value,
                "geography": first.geography,
                "synthetic": first.is_synthetic,
                "count": len(members),
                "first_window_start": members[0].observation_start,
                "last_window_end": members[-1].observation_end,
                "quality": qualities,
            }
        )
    if as_json:
        _emit_json(
            {
                "canonical_query": canonical,
                "queries": [
                    {
                        "id": q.id,
                        "original": q.original_query,
                        "language": q.language,
                        "region": q.region,
                    }
                    for q in query_rows
                ],
                "groups": group_info,
                "observations": [
                    {
                        "id": s.id,
                        "ingestion_run_id": s.ingestion_run_id,
                        "expires_at": s.expires_at,
                        **s.observation.model_dump(mode="json"),
                    }
                    for s in stored[-limit:]
                ],
            }
        )
        return
    if not stored:
        console.print(f"No stored observations for {canonical!r}.")
        return
    if any(s.observation.is_synthetic for s in stored):
        console.print("[bold yellow]Contains SYNTHETIC data - not evidence of real demand.[/]")
    gt = Table(
        "group",
        "provider",
        "measurement",
        "property",
        "scale",
        "batch",
        "gran.",
        "geo",
        "n",
        "window",
        "quality",
        title=f"Comparability groups for {canonical!r}",
    )
    for g in group_info:
        gt.add_row(
            g["comparability_key"],
            g["provider"],
            g["measurement_type"],
            g["search_property"],
            g["scale_type"],
            str(g["normalization_batch"]),
            g["granularity"],
            g["geography"],
            str(g["count"]),
            f"{_fmt(g['first_window_start'])} -> {_fmt(g['last_window_end'])}",
            ", ".join(f"{k}={v}" for k, v in sorted(g["quality"].items())),
        )
    console.print(gt)
    if len(groups) > 1:
        console.print("[yellow]Values in different groups are NOT directly comparable.[/]")
    ot = Table(
        "window start",
        "window end",
        "value",
        "quality",
        "collected",
        "available",
        "group",
        "source",
        title=f"Most recent {min(limit, len(stored))} observations",
    )
    for s in sorted(stored, key=lambda s: s.observation.observation_start)[-limit:]:
        o = s.observation
        ot.add_row(
            _fmt(o.observation_start),
            _fmt(o.observation_end),
            "-" if o.value is None else f"{o.value:g}",
            o.sample_quality.value,
            _fmt(o.collected_at),
            _fmt(o.available_at),
            next(k for k, v in groups.items() if o in v),
            o.source_reference,
        )
    console.print(ot)


@data_app.command("runs")
def data_runs(
    limit: Annotated[int, typer.Option(min=1, max=200)] = 10,
    show_rejections: Annotated[bool, typer.Option("--rejections")] = False,
    as_json: JsonOpt = False,
) -> None:
    """List recent ingestion runs (and optionally their quarantined records)."""
    container = _container(require_db=True)
    with transaction(container.session_factory) as session:
        repo = IngestionRunRepository(session)
        runs = repo.recent(limit)
        rejections = {r.id: repo.rejections(r.id) for r in runs} if show_rejections else {}
        rows: list[dict[str, Any]] = [
            {
                "id": str(r.id),
                "provider": r.provider_name,
                "operation": r.operation,
                "status": r.status.value,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "received": r.records_received,
                "accepted": r.records_accepted,
                "rejected": r.records_rejected,
                "duplicate": r.records_duplicate,
                "withheld": r.records_withheld,
                "quota": r.estimated_quota_used,
                "error_type": r.error_type,
                "error_message": r.error_message,
                "rejections": [
                    {"reason": x.reason_code.value, "detail": x.reason_detail}
                    for x in rejections.get(r.id, [])
                ],
            }
            for r in runs
        ]
    _run(container, _noop)
    if as_json:
        _emit_json(rows)
        return
    table = Table(
        "run",
        "provider",
        "operation",
        "status",
        "started",
        "recv",
        "stored",
        "rej",
        "dup",
        "withheld",
        "quota",
        "error",
        title="Recent ingestion runs",
    )
    for r in rows:
        table.add_row(
            r["id"][:8],
            r["provider"],
            r["operation"],
            r["status"],
            _fmt(r["started_at"]),
            *(
                str(r[k])
                for k in ("received", "accepted", "rejected", "duplicate", "withheld", "quota")
            ),
            r["error_type"] or "",
        )
    console.print(table)
    for r in rows:
        for x in r["rejections"]:
            console.print(f"  {r['id'][:8]} [yellow]{x['reason']}[/]: {escape(x['detail'])}")


@data_app.command("retention")
def data_retention(
    purge: Annotated[bool, typer.Option("--purge", help="Delete expired records now.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show (and optionally purge) records past their source retention period."""
    container = _container(require_db=True)
    service = container.retention()
    report = service.report()
    deleted = service.purge_expired() if purge else None
    _run(container, _noop)
    data: dict[str, Any] = {
        "checked_at": report.checked_at,
        "expired_not_yet_purged": report.expired,
        "videos_expiring_within_days": {
            "days": report.horizon.days,
            "count": report.expiring_videos_within_horizon,
        },
        "deleted": deleted,
    }
    if as_json:
        _emit_json(data)
        return
    table = Table("table", "expired records", "deleted" if purge else "", title="Retention")
    for name, count in sorted(report.expired.items()):
        table.add_row(name, str(count), str(deleted.get(name, 0)) if deleted else "")
    console.print(table)
    console.print(
        f"Videos expiring within {report.horizon.days} days: "
        f"{report.expiring_videos_within_horizon} (refresh with `youtube refresh`)."
    )
    if not purge and any(report.expired.values()):
        console.print("[yellow]Expired records present; run with --purge to delete them.[/]")


# ------------------------------------------------------------------------- quota
@quota_app.command("status")
def quota_status(as_json: JsonOpt = False) -> None:
    """Show locally estimated quota usage for the current reset window."""
    container = _container(require_db=False)
    youtube = container.youtube()
    usage = youtube.quota.status()
    tz = youtube.quota.policy.reset_timezone
    _run(container, _noop)
    rows: list[dict[str, Any]] = [
        {
            "provider": u.provider,
            "bucket": u.bucket,
            "used": u.used,
            "effective_limit": u.effective_limit,
            "daily_limit": u.daily_limit,
            "remaining": u.remaining,
            "server_reported_exhausted": u.server_exhausted,
            "window_start_utc": u.window_start,
            "window_end_utc": u.window_end,
        }
        for u in usage
    ]
    if as_json:
        _emit_json({"store": container.quota_store_kind, "reset_timezone": tz, "buckets": rows})
        return
    table = Table(
        "provider",
        "bucket",
        "used",
        "usable",
        "daily limit",
        "remaining",
        "server exhausted",
        "resets (UTC)",
        title="Estimated quota (local)",
    )
    for r in rows:
        table.add_row(
            r["provider"],
            r["bucket"],
            str(r["used"]),
            str(r["effective_limit"]),
            str(r["daily_limit"]),
            str(r["remaining"]),
            str(r["server_reported_exhausted"]),
            _fmt(r["window_end_utc"]),
        )
    console.print(table)
    console.print(
        f"Resets at midnight {tz}. 'usable' = daily limit minus safety margin. These are local "
        "estimates; Google's Cloud Console quota page is authoritative."
    )


# ------------------------------------------------------------------------- db
@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Apply database migrations (alembic upgrade)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_alembic_ini()))
    cfg.attributes["database_url"] = SETTINGS_FACTORY().database_url.get_secret_value()
    command.upgrade(cfg, revision)
    console.print(f"Database upgraded to {revision}.")


@db_app.command("current")
def db_current() -> None:
    """Show the current schema revision and the latest available revision."""
    container = CONTAINER_FACTORY(SETTINGS_FACTORY())
    ok, detail = check_connection(container.engine)
    container.engine.dispose()
    console.print(f"{detail}; latest available: {_alembic_head()}")
    if not ok:
        raise typer.Exit(3)


# Phase 2 command group (registered last: it uses the helpers defined above).
from trend_agent import cli_discover as _cli_discover  # noqa: E402

app.add_typer(_cli_discover.discover_app, name="discover")
from trend_agent import cli_monitor as _cli_monitor  # noqa: E402

app.add_typer(_cli_monitor.monitor_app, name="monitor")
from trend_agent import cli_detect as _cli_detect  # noqa: E402

app.add_typer(_cli_detect.detect_app, name="detect")
from trend_agent import cli_verify as _cli_verify  # noqa: E402

app.add_typer(_cli_verify.verify_app, name="verify")
from trend_agent import cli_analyze as _cli_analyze  # noqa: E402

app.add_typer(_cli_analyze.analyze_app, name="analyze")
from trend_agent import cli_forecast as _cli_forecast  # noqa: E402

app.add_typer(_cli_forecast.forecast_app, name="forecast")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
from trend_agent import cli_recommend as _cli_recommend  # noqa: E402

app.add_typer(_cli_recommend.recommend_app, name="recommend")
app.add_typer(_cli_recommend.dashboard_app, name="dashboard")

from trend_agent import cli_system as _cli_system  # noqa: E402  (registers Phase 8 commands)

app.add_typer(_cli_system.system_app, name="system")

from trend_agent import cli_evaluate as _cli_evaluate  # noqa: E402  (Phase 8 evaluation)

app.add_typer(_cli_evaluate.evaluate_app, name="evaluate")
