"""``trend-agent github ...``: GitHub repository discovery and release monitoring.

Everything shown here is external developer activity. Stars and forks are cumulative
account actions; nothing here is search interest, search volume or YouTube demand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import TARGET_CATEGORIES, Category, SourceType
from trend_agent.github.client import BUCKET_CORE, BUCKET_SEARCH

github_app = typer.Typer(
    help="GitHub REST API: emerging repositories, releases, developer activity "
    "(never search volume).",
    no_args_is_help=True,
)

NOT_DEMAND = (
    "GitHub data is external developer activity: stars/forks are cumulative account actions, "
    "not people, not search interest and not YouTube demand."
)
STRATEGIES = ("emerging", "active", "releases", "all")


def _provider(container: Any) -> Any:
    from trend_agent.discovery.providers.github import GitHubDiscoveryProvider

    try:
        container.discovery()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None
    provider = container.registry.get("github")
    if not isinstance(provider, GitHubDiscoveryProvider):  # pragma: no cover - wiring
        raise base._fail("GitHub provider is not registered", 2)
    return provider


def _sources(container: Any, categories: list[Category], strategy: str) -> list[Any]:
    out = []
    for s in container.discovery_config().sources:
        if s.provider != "github" or not s.enabled or s.category not in categories:
            continue
        if strategy == "releases" and s.source_type is not SourceType.REPOSITORY:
            continue
        if strategy in ("emerging", "active") and (
            s.source_type is not SourceType.REPOSITORY_SEARCH
            or s.params.get("strategy") != strategy
        ):
            continue
        out.append(s)
    return out


def _usage(provider: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for bucket in (BUCKET_CORE, BUCKET_SEARCH):
        try:
            u = provider.client.quota.usage(bucket)
        except Exception as exc:  # store unavailable
            out[bucket] = {"error": type(exc).__name__}
            continue
        out[bucket] = {
            "used_today": u.used,
            "daily_budget": u.effective_limit,
            "remaining": u.remaining,
            "resets_at": u.window_end.isoformat(),
        }
    return out


# --------------------------------------------------------------------------- status
@github_app.command("status")
def github_status(as_json: base.JsonOpt = False) -> None:
    """Configuration, credential state (never the token), budgets and stored data."""
    from trend_agent.github.models import MEASUREMENT_DEFINITIONS
    from trend_agent.github.service import GitHubReadService

    container = base._container(require_db=False)
    settings = container.settings
    provider = _provider(container)
    report = provider.status()
    sources = [s for s in container.discovery_config().sources if s.provider == "github"]
    try:
        stored: dict[str, Any] = GitHubReadService(container.session_factory).summary()
    except Exception as exc:  # database unavailable: status still answers
        stored = {"error": f"database unavailable ({type(exc).__name__})"}
    data = {
        "provider": "github",
        "status": report.status.value,
        "detail": report.detail,
        "api_enabled": settings.github_api_enabled,
        "credentials": "set" if settings.github_token else "missing (public access)",
        "auth_mode": provider.client.auth_mode(),
        "api_version": provider.client.api_version,
        "base_url": settings.github_api_base_url,
        "capabilities": sorted(c.value for c in provider.descriptor.capabilities),
        "sources": {
            "searches": sum(1 for s in sources if s.source_type is SourceType.REPOSITORY_SEARCH),
            "monitored_repositories": sum(
                1 for s in sources if s.source_type is SourceType.REPOSITORY
            ),
            "enabled": sum(1 for s in sources if s.enabled),
        },
        "local_budgets": _usage(provider),
        "stored": {k: v for k, v in stored.items() if k not in ("definitions", "evidence_classes")},
        "signal_class": "DEVELOPER_ACTIVITY",
        "definitions": MEASUREMENT_DEFINITIONS,
    }
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    table = Table("setting", "value", title="GitHub REST API")
    for key in ("status", "detail", "api_enabled", "credentials", "auth_mode", "api_version"):
        table.add_row(key, escape(str(data[key])))
    table.add_row("capabilities", ", ".join(data["capabilities"]))
    table.add_row(
        "sources",
        f"{data['sources']['searches']} searches, "
        f"{data['sources']['monitored_repositories']} monitored repositories",
    )
    for bucket, u in data["local_budgets"].items():
        table.add_row(
            f"budget {bucket}",
            escape(str(u if "error" in u else f"{u['used_today']}/{u['daily_budget']} today")),
        )
    for key in ("repositories", "observations", "releases", "freshness_hours"):
        if key in data["stored"]:
            table.add_row(f"stored {key}", str(data["stored"][key]))
    base.console.print(table)
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- test
@github_app.command("test")
def github_test(as_json: base.JsonOpt = False) -> None:
    """Bounded live connection test: GET /rate_limit (free) + one search request."""
    from trend_agent.db.repositories.ops import ProviderCheckRepository
    from trend_agent.db.session import transaction
    from trend_agent.github.service import connection_test
    from trend_agent.ops.signals import classify

    container = base._container(require_db=False)
    provider = _provider(container)
    if provider.status().status.value == "disabled":
        container.engine.dispose()
        raise base._fail(f"GitHub is disabled: {provider.status().detail}", 2)
    report = base._run(container, lambda: connection_test(provider.client))
    error = report.pop("error")
    try:
        with transaction(container.session_factory) as s:
            ProviderCheckRepository(s).add(
                provider_name="github",
                now=container.clock(),
                ok=bool(report["ok"]),
                status="available" if report["ok"] else error.provider_status.value,
                detail="live request succeeded"
                if report["ok"]
                else f"{error.error_type}: {error.message}",
                latency_ms=None,
                units_spent=int(report["search_requests"]),
                signal_classes=tuple(c.value for c in classify(provider.descriptor)),
            )
    except Exception:  # pragma: no cover - database unavailable; the test still reports
        report["recorded"] = False
    if as_json:
        base._emit_json(report)
    else:
        colour = "green" if report["ok"] else "red"
        base.console.print(
            f"GitHub connection: [{colour}]{'ok' if report['ok'] else 'failed'}[/] "
            f"(auth mode {report['auth_mode']}, API version {report['api_version']})"
        )
        for name, r in report["rate_limits"].items():
            base.console.print(
                f"  rate limit {name}: {r['remaining']}/{r['limit']} left, resets {r['reset_at']}"
            )
        if report["ok"]:
            base.console.print(
                f"  sample search: {escape(report['sample_query'])} -> "
                f"{report['sample_total_count']} repositories (showing {len(report['sample'])})"
            )
            for r in report["sample"]:
                base.console.print(f"    {escape(r['full_name'])}  {r['html_url']}")
        else:
            base.console.print(f"  [red]{escape(report['error_detail']['message'])}[/]")
    if not report["ok"]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- discover
@github_app.command("discover")
def github_discover(
    category: Annotated[list[Category] | None, typer.Option("--category", "-c")] = None,
    strategy: Annotated[str, typer.Option(help="emerging | active | releases | all")] = "all",
    max_fetches: Annotated[int, typer.Option(min=1, max=100)] = 8,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; fetch nothing.")] = False,
    force: Annotated[bool, typer.Option(help="Ignore minimum polling intervals.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Run GitHub sources through the normal discovery pipeline (bounded by --max-fetches)."""
    from trend_agent.cli_discover import _print_run

    if strategy not in STRATEGIES:
        raise base._fail(f"--strategy must be one of {', '.join(STRATEGIES)}", 2)
    cats = list(category or [Category.AI, Category.TECHNOLOGY])
    bad = [c for c in cats if c not in TARGET_CATEGORIES]
    if bad:
        raise base._fail(f"not a discovery category: {', '.join(c.value for c in bad)}", 2)
    container = base._container(require_db=True)
    _provider(container)
    service = container.discovery()
    sources = _sources(container, cats, strategy)
    if not sources:
        container.engine.dispose()
        raise base._fail("no enabled GitHub sources match these options", 2)
    summary = base._run(
        container,
        lambda: service.run(
            cats, max_fetches=max_fetches, dry_run=dry_run, sources=sources, force=force
        ),
    )
    if as_json:
        base._emit_json(summary.model_dump(mode="json"))
        return
    _print_run(summary, "[github] ")
    base.console.print(f"[dim]{NOT_DEMAND}[/]")
    if summary.status.value == "failed":
        raise typer.Exit(1)


# --------------------------------------------------------------------------- lists
def _read_service(container: Any) -> Any:
    from trend_agent.github.service import GitHubReadService

    try:
        loaded = container.discovery_config()
        from trend_agent.github.config import load_github_settings

        interval = load_github_settings(loaded.config.provider("github").params).momentum
        hours = interval.min_interval_hours
    except ConfigError:
        hours = 6.0
    return GitHubReadService(
        container.session_factory, clock=container.clock, min_interval_hours=hours
    )


def _age(value: str | None, now: datetime) -> str:
    if not value:
        return "-"
    days = (now - datetime.fromisoformat(value)).total_seconds() / 86400
    return f"{days:.0f}d" if days >= 1 else f"{days * 24:.0f}h"


@github_app.command("repositories")
def github_repositories(
    recent: Annotated[
        int | None, typer.Option("--recent", help="Only repositories first seen in N days.")
    ] = None,
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    strategy: Annotated[str | None, typer.Option(help="emerging | active | monitored")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    order: Annotated[str, typer.Option(help="first_seen | score | created")] = "first_seen",
    repository: Annotated[
        str | None, typer.Option("--repo", help="Show one repository (owner/name or ID).")
    ] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Discovered repositories with metadata, provenance and (per repo) momentum."""
    if order not in ("first_seen", "score", "created"):
        raise base._fail("--order must be first_seen, score or created", 2)
    container = base._container(require_db=True)
    svc = _read_service(container)
    if repository:
        detail = svc.repository_detail(repository)
        container.engine.dispose()
        if detail is None:
            raise base._fail(f"repository {repository!r} has not been discovered", 1)
        if as_json:
            base._emit_json(detail)
            return
        _print_detail(detail)
        return
    data = svc.repositories(
        days=recent, category=category, strategy=strategy, limit=limit, order=order
    )
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    now = container.clock()
    table = Table(
        "repository", "category", "strategy", "created", "stars*", "lang", "score", "flags",
        title=f"GitHub repositories ({data['total']} total)",
    )  # fmt: skip
    for r in data["items"]:
        table.add_row(
            escape(r["full_name"]),
            r["category"],
            ",".join(r["strategies"]),
            _age(r["created_at"], now),
            str(r["stargazers_count"]),
            escape(r["language"] or "-"),
            f"{r['discovery_score']:.0f}",
            escape(",".join(r["quality_flags"]) or "-"),
        )
    base.console.print(table)
    base.console.print(
        "[dim]* stars: cumulative count at the last observation, not growth. Score ranks "
        "discoveries (recency, relevance, description, adoption); it is not demand. "
        f"{NOT_DEMAND}[/]"
    )


def _print_detail(d: dict[str, Any]) -> None:
    base.console.print(f"[bold]{escape(d['full_name'])}[/]  {d['html_url']}")
    base.console.print(f"  {escape(d['description'] or '(no description)')}")
    base.console.print(
        f"  created {d['created_at']}  language {d['language']}  topics "
        f"{escape(', '.join(d['topics']))}"
    )
    m = d["momentum"]
    base.console.print(
        f"  momentum [{m['signal_label']}]: status={m['status']} observations="
        f"{m['observations']} interval_h={m['interval_hours']} star_delta={m['star_delta']} "
        f"stars/day={m['stars_per_day']}"
    )
    for note in m["notes"]:
        base.console.print(f"    note: {escape(note)}")
    ea = d["external_attention"]
    base.console.print(
        f"  external attention: {ea['independent_publishers']} independent publisher(s) "
        f"({', '.join(ea['providers']) or 'none'})"
    )
    base.console.print(
        f"  direct YouTube search interest: {d['direct_youtube_search_interest']['detail']}"
    )
    for k in d["keyword_candidates"][:10]:
        base.console.print(f"  KEYWORD_HYPOTHESIS: {escape(k['query'])} ({k['status']})")
    for r in d["releases"][:5]:
        base.console.print(f"  VERIFIED_RELEASE: {escape(r['tag_name'])} {r['published_at']}")


@github_app.command("releases")
def github_releases(
    recent: Annotated[
        int | None, typer.Option("--recent", help="Only releases published in the last N days.")
    ] = None,
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    as_json: base.JsonOpt = False,
) -> None:
    """Releases seen on monitored repositories (version, level, quoted breaking changes)."""
    container = base._container(require_db=True)
    data = _read_service(container).releases(days=recent, category=category, limit=limit)
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    now = container.clock()
    table = Table(
        "repository", "tag", "version", "level", "published", "breaking", "quoted lines",
        title=f"GitHub releases ({data['total']} total)",
    )  # fmt: skip
    for r in data["items"]:
        table.add_row(
            escape(r["repository"]),
            escape(r["tag_name"]),
            escape(r["version"] or "-"),
            r["release_level"],
            _age(r["published_at"], now),
            "yes" if r["is_breaking"] else "no",
            str(len(r["breaking_change_lines"]) + len(r["new_capability_lines"])),
        )
    base.console.print(table)
    base.console.print(
        "[dim]Versions and dates come from GitHub; release-note lines are quoted, never "
        "summarised.[/]"
    )


# --------------------------------------------------------------------------- quota
@github_app.command("quota")
def github_quota(
    live: Annotated[
        bool, typer.Option("--live", help="Also ask GitHub (GET /rate_limit; free).")
    ] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Local daily budgets and (with --live) GitHub's own rate-limit state."""
    container = base._container(require_db=False)
    provider = _provider(container)
    data: dict[str, Any] = {"local_budgets": _usage(provider), "auth_mode": None}
    if live:
        from trend_agent.providers.base import RequestContext
        from trend_agent.providers.errors import ProviderError

        async def ask() -> dict[str, Any]:
            try:
                return {"resources": await provider.client.rate_limit(RequestContext())}
            except ProviderError as exc:
                return {"error": exc.to_dict()}

        data["github"] = base._run(container, ask)
    else:
        container.engine.dispose()
    data["auth_mode"] = provider.client.auth_mode()
    if as_json:
        base._emit_json(data)
        return
    table = Table("bucket", "used today", "daily budget", "remaining", "resets", title="Local")
    for bucket, u in data["local_budgets"].items():
        if "error" in u:
            table.add_row(bucket, "?", "?", "?", u["error"])
        else:
            table.add_row(
                bucket, str(u["used_today"]), str(u["daily_budget"]), str(u["remaining"]),
                u["resets_at"],
            )  # fmt: skip
    base.console.print(table)
    gh = data.get("github")
    if gh:
        if "error" in gh:
            base.console.print(f"[red]GitHub: {escape(gh['error']['message'])}[/]")
        for name, r in (gh.get("resources") or {}).items():
            base.console.print(
                f"GitHub {name}: {r['remaining']}/{r['limit']} left, resets {r['reset_at']}"
            )
    base.console.print(f"auth mode: {data['auth_mode']}")
