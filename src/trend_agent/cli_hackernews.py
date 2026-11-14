"""``trend-agent hackernews ...``: Hacker News stories from the official Firebase API.

Everything shown here is community discussion activity. Points and comments are
cumulative community actions; nothing here is search interest, search volume or YouTube
demand. The official API has no keyword search: lists are read and stories classified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import TARGET_CATEGORIES, Category
from trend_agent.hackernews.models import LIST_BY_TYPE, LIST_LABEL, LISTS

hackernews_app = typer.Typer(
    help="Hacker News (official Firebase API): AI, developer-technology and gaming stories "
    "(community activity, never search volume).",
    no_args_is_help=True,
)

NOT_DEMAND = (
    "Hacker News points/comments are community discussion activity: not views, not search "
    "interest and not YouTube demand."
)
PROVIDER = "hackernews_firebase"
TYPES = tuple(LIST_BY_TYPE)  # new, top, best, show, ask
ListTypeOpt = Annotated[
    list[str] | None,
    typer.Option("--type", "--list", "-t", help="Story list: new | top | best | show | ask."),
]


def _provider(container: Any) -> Any:
    from trend_agent.discovery.providers.hackernews_firebase import HackerNewsDiscoveryProvider

    try:
        container.discovery()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None
    provider = container.registry.get(PROVIDER)
    if not isinstance(provider, HackerNewsDiscoveryProvider):  # pragma: no cover - wiring
        raise base._fail("Hacker News provider is not registered", 2)
    return provider


def _lists(types: list[str] | None) -> list[str]:
    if not types:
        return list(LISTS)
    bad = [t for t in types if t not in LIST_BY_TYPE]
    if bad:
        raise base._fail(f"--type must be one of {', '.join(TYPES)} (got {', '.join(bad)})", 2)
    return [LIST_BY_TYPE[t] for t in types]


def _usage(provider: Any) -> dict[str, Any]:
    from trend_agent.hackernews.client import BUCKET

    try:
        u = provider.client.quota.usage(BUCKET)
    except Exception as exc:  # store unavailable
        return {"error": type(exc).__name__}
    return {
        "used_today": u.used,
        "daily_budget": u.effective_limit,
        "remaining": u.remaining,
        "resets_at": u.window_end.isoformat(),
    }


def _read_service(container: Any) -> Any:
    from trend_agent.hackernews.service import HackerNewsReadService

    return HackerNewsReadService(container.session_factory, clock=container.clock)


def _age(value: str | None, now: datetime) -> str:
    if not value:
        return "-"
    hours = (now - datetime.fromisoformat(value)).total_seconds() / 3600
    return f"{hours / 24:.0f}d" if hours >= 48 else f"{hours:.0f}h"


def _rate(value: Any) -> str:
    return "-" if value is None else f"{value:+.1f}"


def _story_table(items: list[dict[str, Any]], title: str, now: datetime) -> Table:
    table = Table(
        "id", "title", "kind", "category", "lists", "age", "points*", "comments*", "pts/h",
        "priority", "momentum", title=title,
    )  # fmt: skip
    for r in items:
        cat = r["primary_category"] + (
            f"/{r['secondary_category']}" if r["secondary_category"] else ""
        )
        table.add_row(
            str(r["story_id"]),
            escape(r["title"][:70]),
            r["kind"],
            cat + ("?" if r["ambiguous"] else ""),
            ",".join(r["lists"]),
            _age(r["published_at"], now),
            str(r["score"]),
            str(r["comments"]),
            _rate(r.get("score_per_hour")),
            f"{r['priority_score']:.0f}",
            r["momentum_state"],
        )
    return table


def _footer() -> None:
    base.console.print(
        "[dim]* points/comments: cumulative at the last observation, not growth. pts/h is "
        "measured between stored observations only; '?' marks an ambiguous category. Priority "
        f"ranks what to look at; it is not demand. {NOT_DEMAND}[/]"
    )


# --------------------------------------------------------------------------- status
@hackernews_app.command("status")
def hackernews_status(as_json: base.JsonOpt = False) -> None:
    """Configuration, provider health, local request budget and stored data."""
    container = base._container(require_db=False)
    settings = container.settings
    provider = _provider(container)
    report = provider.status()
    sources = [s for s in container.discovery_config().sources if s.provider == PROVIDER]
    try:
        stored: dict[str, Any] = _read_service(container).summary()
    except Exception as exc:  # database unavailable: status still answers
        stored = {"error": f"database unavailable ({type(exc).__name__})"}
    data = {
        "provider": PROVIDER,
        "status": report.status.value,
        "detail": report.detail,
        "api_enabled": settings.hackernews_api_enabled,
        "scheduled_discovery": settings.scheduled_discovery,
        "credentials": "none required",
        "base_url": provider.client.base_url,
        "capabilities": sorted(c.value for c in provider.descriptor.capabilities),
        "sources": [
            {
                "key": s.key,
                "list": LIST_LABEL.get(s.reference, s.reference),
                "enabled": s.enabled,
                "poll_interval_minutes": s.min_poll_interval_minutes,
                "max_items": s.params.get("max_items"),
            }
            for s in sources
        ],
        "refresh": provider.settings.refresh.model_dump(),
        "local_budget": _usage(provider),
        "stored": {k: v for k, v in stored.items() if k not in ("definitions", "evidence_classes")},
        "signal_class": "COMMUNITY_ACTIVITY",
        "caveat": NOT_DEMAND,
    }
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    table = Table("setting", "value", title="Hacker News (official Firebase API)")
    for key in (
        "status",
        "detail",
        "api_enabled",
        "scheduled_discovery",
        "credentials",
        "base_url",
    ):
        table.add_row(key, escape(str(data[key])))
    table.add_row("capabilities", ", ".join(data["capabilities"]))
    for s in data["sources"]:
        table.add_row(
            f"list {s['list']}",
            f"every {s['poll_interval_minutes']} min, up to {s['max_items']} stories"
            + ("" if s["enabled"] else " (disabled)"),
        )
    r = data["refresh"]
    table.add_row(
        "refresh tiers",
        f"high {r['high_priority_minutes']} / normal {r['normal_priority_minutes']} / low "
        f"{r['low_priority_minutes']} min, stop after {r['stop_after_hours']} h",
    )
    u = data["local_budget"]
    table.add_row(
        "local budget",
        escape(str(u if "error" in u else f"{u['used_today']}/{u['daily_budget']} requests today")),
    )
    st = data["stored"]
    for key in ("stories", "active_stories", "observations", "freshness_hours"):
        if key in st:
            table.add_row(f"stored {key}", str(st[key]))
    for h in st.get("sources", []):
        table.add_row(
            f"health {LIST_LABEL.get(h['list'], h['list'])}",
            f"{h['health']} (last success {h['last_success_at'] or 'never'}"
            + (f", error: {escape(h['last_error'][:80])}" if h.get("last_error") else "")
            + ")",
        )
    base.console.print(table)
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- test
@hackernews_app.command("test")
def hackernews_test(as_json: base.JsonOpt = False) -> None:
    """Bounded live test of every endpoint used (about 10 requests, nothing stored)."""
    from trend_agent.db.repositories.ops import ProviderCheckRepository
    from trend_agent.db.session import transaction
    from trend_agent.hackernews.service import connection_test
    from trend_agent.ops.signals import classify

    container = base._container(require_db=False)
    provider = _provider(container)
    if provider.status().status.value == "disabled":
        container.engine.dispose()
        raise base._fail(f"Hacker News is disabled: {provider.status().detail}", 2)
    report = base._run(container, lambda: connection_test(provider.client))
    error = report.pop("error")
    try:
        with transaction(container.session_factory) as s:
            ProviderCheckRepository(s).add(
                provider_name=PROVIDER,
                now=container.clock(),
                ok=bool(report["ok"]),
                status="available" if report["ok"] else error.provider_status.value,
                detail="live requests succeeded"
                if report["ok"]
                else f"{error.error_type}: {error.message}"
                if error
                else "no story records returned",
                latency_ms=None,
                units_spent=int(report["requests_sent"]),
                signal_classes=tuple(c.value for c in classify(provider.descriptor)),
            )
    except Exception:  # pragma: no cover - database unavailable; the test still reports
        report["recorded"] = False
    if as_json:
        base._emit_json(report)
    else:
        colour = "green" if report["ok"] else "red"
        base.console.print(
            f"Hacker News connection: [{colour}]{'ok' if report['ok'] else 'failed'}[/] "
            f"({report['requests_sent']} requests to {report['base_url']})"
        )
        for endpoint, state in report["endpoints"].items():
            base.console.print(f"  {endpoint}: {escape(str(state))}")
        base.console.print(f"  max item id: {report.get('max_item')}")
        base.console.print(
            "  lists: "
            + ", ".join(f"{LIST_LABEL[k]} {v}" for k, v in report["lists"].items())
            + " story IDs"
        )
        for r in report["sample"]:
            base.console.print(
                f"    {r['story_id']}  {escape(r['title'][:80])}  ({r['score']} points, "
                f"{r['comments']} comments, {r['category']})"
            )
        if not report["ok"] and report.get("error_detail"):
            base.console.print(f"  [red]{escape(report['error_detail']['message'])}[/]")
    if not report["ok"]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- discover
@hackernews_app.command("discover")
def hackernews_discover(
    category: Annotated[
        list[Category] | None,
        typer.Option(
            "--category", "-c", help="Show stories of this category (ai, technology, gaming)."
        ),
    ] = None,
    types: ListTypeOpt = None,
    max_fetches: Annotated[int, typer.Option(min=1, max=20)] = 5,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; fetch nothing.")] = False,
    force: Annotated[bool, typer.Option(help="Ignore minimum polling intervals.")] = False,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 25,
    as_json: base.JsonOpt = False,
) -> None:
    """Read story lists through the normal discovery pipeline and show what was found.

    The official API has no category or keyword search: every selected list is read and
    each story is classified; --category filters the stories shown, it never forces any.
    """
    from trend_agent.cli_discover import _print_run
    from trend_agent.hackernews.service import run_list_discovery

    cats = list(category or [Category.AI, Category.TECHNOLOGY])
    bad = [c for c in cats if c not in TARGET_CATEGORIES]
    if bad:
        raise base._fail(f"not a discovery category: {', '.join(c.value for c in bad)}", 2)
    lists = _lists(types)
    container = base._container(require_db=True)
    provider = _provider(container)
    if provider.status().status.value == "disabled":
        container.engine.dispose()
        raise base._fail(f"Hacker News is disabled: {provider.status().detail}", 2)
    service = container.discovery()
    sources = [
        s
        for s in container.discovery_config().sources
        if s.provider == PROVIDER and s.enabled and s.reference in lists
    ]
    if not sources:
        container.engine.dispose()
        raise base._fail("no enabled Hacker News sources match these options", 2)
    started = container.clock()
    summaries = base._run(
        container,
        lambda: run_list_discovery(
            service,
            sources,
            config=container.discovery_config().config,
            max_fetches=max_fetches,
            dry_run=dry_run,
            force=force,
        ),
    )
    found: dict[str, Any] = {"total": 0, "items": []}
    if not dry_run:
        svc = _read_service(container)
        merged: dict[int, dict[str, Any]] = {}
        for cat in cats:
            data = svc.stories(
                seen_since=started, category=cat.value, limit=limit, order="priority"
            )
            for item in data["items"]:
                merged[item["story_id"]] = item
        found = {
            "total": len(merged),
            "items": sorted(merged.values(), key=lambda r: -r["priority_score"])[:limit],
        }
    container.engine.dispose()
    if as_json:
        base._emit_json(
            {
                "runs": [x.model_dump(mode="json") for x in summaries],
                "categories": [c.value for c in cats],
                "stories_seen_this_run": found,
                "caveat": NOT_DEMAND,
            }
        )
        return
    for summary in summaries:
        _print_run(summary, "[hackernews] ")
    if not dry_run:
        base.console.print(
            _story_table(
                found["items"],
                f"{', '.join(c.value for c in cats)} stories seen in this run ({found['total']})",
                container.clock(),
            )
        )
        _footer()
    if summaries and all(x.status.value == "failed" for x in summaries):
        raise typer.Exit(1)


# --------------------------------------------------------------------------- stories
@hackernews_app.command("stories")
def hackernews_stories(
    types: ListTypeOpt = None,
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    days: Annotated[int | None, typer.Option(help="Only stories published in N days.")] = None,
    order: Annotated[
        str, typer.Option(help="published | priority | score | comments | momentum")
    ] = "published",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    story: Annotated[int | None, typer.Option("--story", help="Show one story by ID.")] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Read the list now (bounded, nothing stored) instead."),
    ] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Stored stories (with provenance and momentum), or one list read live with --live."""
    if story is not None:
        container = base._container(require_db=True)
        detail = _read_service(container).story_detail(story)
        container.engine.dispose()
        if detail is None:
            raise base._fail(f"story {story} has not been discovered", 1)
        if as_json:
            base._emit_json(detail)
            return
        _print_detail(detail)
        return
    lists = _lists(types)
    if live:
        _live_stories(lists, limit, category, as_json)
        return
    container = base._container(require_db=True)
    svc = _read_service(container)
    out: dict[str, Any] = {}
    try:
        for name in lists if types else [None]:
            out[LIST_LABEL.get(name or "", "all")] = svc.stories(
                days=days,
                list_name=name,
                category=category.value if category else None,
                order=order,
                limit=limit,
            )
    except ValueError as exc:
        container.engine.dispose()
        raise base._fail(str(exc), 2) from None
    container.engine.dispose()
    if as_json:
        base._emit_json(out)
        return
    now = container.clock()
    for label, data in out.items():
        base.console.print(_story_table(data["items"], f"{label} stories ({data['total']})", now))
    _footer()


def _live_stories(lists: list[str], limit: int, category: Category | None, as_json: bool) -> None:
    from trend_agent.hackernews.classify import classify_story
    from trend_agent.providers.base import RequestContext
    from trend_agent.providers.errors import ProviderError

    container = base._container(require_db=False)
    provider = _provider(container)
    n = max(1, min(limit, provider.settings.client.max_items_per_list))

    async def read() -> dict[str, Any]:
        ctx = RequestContext()
        out: dict[str, Any] = {}
        for name in lists:
            try:
                ids = await provider.client.story_ids(name, ctx)
                batch = await provider.client.items(ids[:n], ctx)
            except ProviderError as exc:
                out[LIST_LABEL[name]] = {"error": exc.to_dict()}
                continue
            rows = []
            for rank, sid in enumerate(ids[:n], start=1):
                item = batch.items.get(sid)
                if item is None:
                    continue
                c = classify_story(item, provider.settings.classifier)
                if category and category not in (c.primary, c.secondary):
                    continue
                rows.append(
                    {
                        "rank": rank,
                        "story_id": item.id,
                        "title": c.clean_title,
                        "kind": c.kind,
                        "category": c.primary.value,
                        "secondary_category": c.secondary.value if c.secondary else None,
                        "relevance": c.relevance,
                        "score": item.score,
                        "comments": item.descendants,
                        "published_at": item.published_at.isoformat()
                        if item.published_at
                        else None,
                        "discussion_url": item.discussion_url,
                        "linked_url": item.url,
                        "flags": c.flags,
                    }
                )
            out[LIST_LABEL[name]] = {
                "read": len(ids[:n]),
                "skipped": dict(batch.skipped),
                "failed": dict(batch.failed),
                "items": rows,
            }
        return out

    data = base._run(container, read)
    if as_json:
        base._emit_json({"live": True, "lists": data, "caveat": NOT_DEMAND})
        return
    now = container.clock()
    for label, d in data.items():
        if "error" in d:
            base.console.print(f"[red]{label}: {escape(d['error']['message'])}[/]")
            continue
        table = Table(
            "#", "id", "title", "category", "age", "points*", "comments*",
            title=f"{label} stories, live (first {d['read']}; {len(d['skipped'])} skipped)",
        )  # fmt: skip
        for r in d["items"]:
            table.add_row(
                str(r["rank"]),
                str(r["story_id"]),
                escape(r["title"][:80]),
                r["category"],
                _age(r["published_at"], now),
                str(r["score"]),
                str(r["comments"]),
            )
        base.console.print(table)
    base.console.print(f"[dim]Live read, nothing stored. {NOT_DEMAND}[/]")


def _print_detail(d: dict[str, Any]) -> None:
    base.console.print(f"[bold]{escape(d['title'])}[/]  ({d['kind']}, {d['subject']})")
    base.console.print(f"  discussion: {d['discussion_url']}")
    if d["linked_url"]:
        base.console.print(f"  link: {escape(d['linked_url'])}")
    base.console.print(
        f"  by {escape(d['author'] or '?')}, published {d['published_at']}; category "
        f"{d['primary_category']}"
        + (f" / {d['secondary_category']}" if d["secondary_category"] else "")
        + (" (ambiguous)" if d["ambiguous"] else "")
        + f", relevance {d['relevance']}"
    )
    m = d["momentum"] or {}
    base.console.print(
        f"  momentum [{m.get('signal_label', '-')}]: status={m.get('status')} "
        f"observations={m.get('observations')} interval_h={m.get('interval_hours')} "
        f"points_delta={m.get('score_delta')} points/h={m.get('score_per_hour')} "
        f"comments/h={m.get('comments_per_hour')}"
    )
    for note in m.get("notes", []):
        base.console.print(f"    note: {escape(note)}")
    base.console.print(f"  priority {d['priority_score']} ({d['refresh_tier']} refresh tier):")
    for name, comp in (d["priority_components"] or {}).items():
        if name.startswith("_"):
            continue
        base.console.print(
            f"    {name}: {comp['value']} x {comp['weight']} -> {comp['contribution']}  "
            f"[dim]{escape(comp['basis'])}[/]"
        )
    if d["entity"]:
        e = d["entity"]
        base.console.print(f"  entity: {escape(e['name'])} ({e['type']}, {e['status']})")
    if d["reposts"]:
        base.console.print(
            "  other stories linking the same article (one origin, not independent): "
            + ", ".join(str(r["story_id"]) for r in d["reposts"])
        )
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
    base.console.print(f"  observations: {len(d['observations'])}")
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- trending
@hackernews_app.command("trending")
def hackernews_trending(
    hours: Annotated[int, typer.Option(min=1, max=168)] = 48,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
    as_json: base.JsonOpt = False,
) -> None:
    """Stories with the fastest *measured* discussion growth (needs repeat observations)."""
    container = base._container(require_db=True)
    data = _read_service(container).trending(hours=hours, limit=limit)
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        _story_table(
            data["items"],
            f"Hacker News discussion momentum, last {hours} h ({data['measured']} measured)",
            container.clock(),
        )
    )
    if data["awaiting_repeat_observation"]:
        base.console.print(
            f"{data['awaiting_repeat_observation']} more stories have one observation (or "
            "two too close together): not ranked, no growth reported."
        )
    _footer()


# --------------------------------------------------------------------------- stats
@hackernews_app.command("stats")
def hackernews_stats(as_json: base.JsonOpt = False) -> None:
    """Stored stories and observations by category, list and kind; source health."""
    container = base._container(require_db=True)
    data = _read_service(container).summary()
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    table = Table("measure", "value", title="Hacker News data")
    for key in (
        "stories", "active_stories", "rising_stories", "observations", "refresh_observations",
        "entities_linked", "last_observed_at", "last_refresh_at", "next_refresh_at",
        "freshness_hours",
    ):  # fmt: skip
        table.add_row(key, str(data.get(key)))
    for group in ("by_category", "by_list", "by_kind"):
        table.add_row(
            group,
            ", ".join(f"{LIST_LABEL.get(k, k)} {v}" for k, v in sorted(data[group].items())) or "-",
        )
    base.console.print(table)
    health = Table("list", "health", "every", "last success", "failures", "cursor", "last error")
    for h in data["sources"]:
        health.add_row(
            LIST_LABEL.get(h["list"], h["list"]),
            h["health"],
            f"{h['poll_interval_minutes']} min",
            h["last_success_at"] or "never",
            str(h["consecutive_failures"]),
            h["cursor"] or "-",
            escape((h["last_error"] or "-")[:60]),
        )
    base.console.print(health)
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- refresh
@hackernews_app.command("refresh")
def hackernews_refresh(
    max_items: Annotated[int, typer.Option(min=1, max=500)] = 30,
    no_updates: Annotated[
        bool, typer.Option("--no-updates", help="Skip the /updates.json hint.")
    ] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Re-observe tracked stories that are due (bounded; builds momentum)."""
    container = base._container(require_db=True)
    try:
        refresher = container.hackernews_refresher()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None
    report = base._run(
        container, lambda: refresher.run(max_items=max_items, use_updates=not no_updates)
    )
    if as_json:
        base._emit_json(report)
        return
    if "skipped" in report:
        base.console.print(f"refresh skipped: {report['skipped']}")
        return
    base.console.print(
        f"refresh: {report['observed']} stories re-observed of {report['selected']} selected "
        f"({report['pulled_forward_by_updates']} pulled forward by /updates.json), "
        f"{report['requests']} requests"
    )
    for key in ("deactivated", "failed"):
        if report[key]:
            base.console.print(f"  {key}: {report[key]}")
    for err in report["errors"]:
        base.console.print(f"  [yellow]{escape(err)}[/]")
    base.console.print(f"[dim]{NOT_DEMAND}[/]")
