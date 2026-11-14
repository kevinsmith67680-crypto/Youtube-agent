"""``trend-agent twitch ...``: Twitch Helix API (top games, live streams, attention).

Everything shown here is livestream activity on Twitch (external gaming attention).
Viewer numbers are concurrent viewers of the streams actually returned; nothing here is
YouTube search interest, search volume or demand. The client secret and the app access
token are never printed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.discovery.config import ConfigError

twitch_app = typer.Typer(
    help="Twitch Helix API: top games, live streams, observed viewers, attention momentum "
    "(external gaming attention, never search volume).",
    no_args_is_help=True,
)

NOT_DEMAND = (
    "Twitch numbers are livestream activity on another platform: observed concurrent "
    "viewers of the streams returned, not search interest and not YouTube demand."
)
PROVIDER = "twitch"
AutoRefresh = Annotated[
    bool | None,
    typer.Option(
        "--refresh/--no-refresh",
        help="Poll Twitch now (recorded). Default: only when the stored data is older than "
        "its polling interval.",
    ),
]


def _provider(container: Any) -> Any:
    from trend_agent.discovery.providers.twitch import TwitchDiscoveryProvider

    try:
        container.discovery()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None
    provider = container.registry.get(PROVIDER)
    if not isinstance(provider, TwitchDiscoveryProvider):  # pragma: no cover - wiring
        raise base._fail("Twitch provider is not registered", 2)
    return provider


def _require_live(container: Any, provider: Any) -> None:
    rep = provider.status()
    if rep.status.value in ("disabled", "not_configured"):
        container.engine.dispose()
        raise base._fail(f"Twitch is {rep.status.value}: {rep.detail}", 2)


def _usage(provider: Any) -> dict[str, Any]:
    from trend_agent.twitch.client import BUCKET

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


def _read(container: Any) -> Any:
    from trend_agent.twitch.service import TwitchReadService

    return TwitchReadService(
        container.session_factory, container.twitch_settings(), clock=container.clock
    )


def _stale(container: Any, kind: str, minutes: int, game_id: str | None = None) -> bool:
    from trend_agent.db.repositories.twitch import TwitchRepository
    from trend_agent.db.session import transaction

    with transaction(container.session_factory) as s:
        last = TwitchRepository(s).latest_snapshot(kind, game_id)
        return last is None or container.clock() - last.observed_at >= timedelta(minutes=minutes)


def _num(v: Any) -> str:
    return "-" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def _change(v: Any) -> str:
    if v is None:
        return "-"
    return f"+{v}" if v > 0 else str(v)


def _footer() -> None:
    base.console.print(
        "[dim]Ranks: positions in Get Top Games (no viewer numbers from that endpoint). "
        "Observed viewers: sum over the streams returned; 'partial' means the list was "
        f"not read to its end, so it is not a game total. {NOT_DEMAND}[/]"
    )


async def _resolve(container: Any, name: str) -> Any:
    return await container.twitch_monitor(trigger="cli").resolve(name)


# --------------------------------------------------------------------------- status
@twitch_app.command("status")
def twitch_status(as_json: base.JsonOpt = False) -> None:
    """Configuration, credential state (never the values), token state, budgets, data."""
    container = base._container(require_db=False)
    settings = container.settings
    provider = _provider(container)
    report = provider.status()
    tw = container.twitch_settings()
    try:
        stored: dict[str, Any] = _read(container).summary()
        from trend_agent.db.repositories.twitch import TwitchRepository
        from trend_agent.db.session import transaction

        with transaction(container.session_factory) as s:
            repo = TwitchRepository(s)
            monitored = []
            for m in repo.monitored():
                # auto-selected games have no requested name: show the catalog name
                row = None if m.requested_name else repo.game(m.game_id)
                name = m.requested_name or (row.name if row else None)
                monitored.append(
                    {"game_id": m.game_id, "name": name, "tier": m.tier, "origin": m.origin}
                )
    except Exception as exc:  # database unavailable: status still answers
        stored = {"error": f"database unavailable ({type(exc).__name__})"}
        monitored = []
    data = {
        "provider": PROVIDER,
        "status": report.status.value,
        "detail": report.detail,
        "api_enabled": settings.twitch_api_enabled,
        "scheduled_discovery": settings.scheduled_discovery,
        "client_id": "set" if settings.twitch_client_id else "missing",
        # reported as set/missing only, never the value
        "client_secret": "set"  # pragma: allowlist secret
        if settings.twitch_client_secret
        else "missing",
        "token": provider.client.tokens.state(),
        "capabilities": sorted(c.value for c in provider.descriptor.capabilities),
        "cadence_minutes": tw.cadence.model_dump(),
        "collection": tw.collection.model_dump(),
        "retention": tw.retention.model_dump(),
        "monitored_games": monitored,
        "local_budget": _usage(provider),
        "stored": {k: v for k, v in stored.items() if k not in ("definitions", "evidence_classes")},
        "signal_class": "LIVESTREAM_ACTIVITY",
        "caveat": NOT_DEMAND,
    }
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    table = Table("setting", "value", title="Twitch Helix API")
    for key in (
        "status",
        "detail",
        "api_enabled",
        "scheduled_discovery",
        "client_id",
        "client_secret",
    ):
        table.add_row(key, escape(str(data[key])))
    t = data["token"]
    table.add_row(
        "app token",
        "none requested in this process (requested on first use)"
        if not t["has_token"]
        else f"held in memory, expires in {t['expires_in_hours']} h ({t['validation']})",
    )
    table.add_row("capabilities", ", ".join(data["capabilities"]))
    c = data["cadence_minutes"]
    table.add_row(
        "cadence",
        f"top games {c['top_games_minutes']} min, top streams {c['top_streams_minutes']} min, "
        f"monitored games {c['high_priority_game_minutes']}/{c['low_priority_game_minutes']} min",
    )
    r = data["retention"]
    table.add_row(
        "retention",
        f"{r['hours']} h (Twitch Developer Agreement cache limit"
        + (", extended storage authorised" if r["extended_storage_authorized"] else "")
        + ")",
    )
    table.add_row(
        "monitored games",
        ", ".join(f"{m['name'] or m['game_id']} ({m['tier']}, {m['origin']})" for m in monitored)
        or "-",
    )
    u = data["local_budget"]
    table.add_row(
        "local budget",
        escape(str(u if "error" in u else f"{u['used_today']}/{u['daily_budget']} requests today")),
    )
    st = data["stored"]
    for key in ("games", "games_real", "rising_games", "rank_observations", "viewer_observations"):
        if key in st:
            table.add_row(f"stored {key}", str(st[key]))
    for kind, f in (st.get("freshness") or {}).items():
        table.add_row(f"latest {kind}", f"{f['last_observed_at']} ({f['age_minutes']} min ago)")
    base.console.print(table)
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- test
@twitch_app.command("test")
def twitch_test(as_json: base.JsonOpt = False) -> None:
    """Bounded live test: app token, top 20 games, 20 live streams, Fortnite lookup and
    20 Fortnite streams (about 5 Helix requests; nothing stored except the check record)."""
    from trend_agent.db.repositories.ops import ProviderCheckRepository
    from trend_agent.db.session import transaction
    from trend_agent.ops.signals import classify
    from trend_agent.twitch.service import connection_test

    container = base._container(require_db=False)
    provider = _provider(container)
    _require_live(container, provider)
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
                else "no data returned",
                latency_ms=None,
                units_spent=int(report.get("requests_sent") or 0),
                signal_classes=tuple(c.value for c in classify(provider.descriptor)),
            )
    except Exception:  # pragma: no cover - database unavailable; the test still reports
        report["recorded"] = False
    if as_json:
        base._emit_json(report)
    else:
        colour = "green" if report["ok"] else "red"
        base.console.print(
            f"Twitch connection: [{colour}]{'ok' if report['ok'] else 'failed'}[/] "
            f"({report.get('requests_sent', 0)} Helix requests)"
        )
        for step, state in report["steps"].items():
            base.console.print(f"  {step}: {escape(str(state))}")
        t = report.get("token") or {}
        base.console.print(
            f"  app token: {t.get('validation')}, expires in {t.get('expires_in_hours')} h "
            "(value never shown)"
        )
        rl = report.get("rate_limit") or {}
        base.console.print(f"  rate limit: {rl.get('remaining')}/{rl.get('limit')} points left")
        for g in report["top_games"][:10]:
            base.console.print(f"    #{g['rank']} {escape(g['name'])} (id {g['game_id']})")
        if report.get("fortnite"):
            f = report["fortnite"]
            base.console.print(
                f"  Fortnite: id {f['game_id']}, IGDB {f['igdb_id']}; "
                f"{len(report['fortnite_streams'])} streams sampled, "
                f"{_num(report.get('fortnite_sampled_viewers'))} observed viewers "
                f"({report.get('fortnite_sample_status')} sample)"
            )
        if not report["ok"] and report.get("error_detail"):
            base.console.print(f"  [red]{escape(report['error_detail']['message'])}[/]")
        base.console.print(f"[dim]{NOT_DEMAND}[/]")
    if not report["ok"]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- top games
@twitch_app.command("top-games")
def twitch_top_games(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    max_rank: Annotated[int | None, typer.Option(help="Only games ranked at or above.")] = None,
    games_only: Annotated[
        bool, typer.Option("--games-only", help="Hide non-game categories (Just Chatting, ...).")
    ] = False,
    refresh: AutoRefresh = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Current ranking (Get Top Games) with the change since the previous stored poll."""
    container = base._container(require_db=True)
    provider = _provider(container)
    tw = container.twitch_settings()
    polled = None
    if refresh or (
        refresh is None and _stale(container, "top_games", tw.cadence.top_games_minutes)
    ):
        _require_live(container, provider)
        monitor = container.twitch_monitor(trigger="cli")

        async def poll() -> dict[str, Any]:
            out = await monitor.poll_top_games()
            from trend_agent.db.session import transaction

            with transaction(container.session_factory) as s:
                monitor.assess(s, out["game_ids"], container.clock())
            return out

        polled = base._run(container, poll)
        polled.pop("game_ids", None)
    data = _read(container).top_games(
        limit=limit, max_rank=max_rank, category="game" if games_only else None
    )
    container.engine.dispose()
    if polled:
        data["polled_now"] = polled
    if as_json:
        base._emit_json(data)
        return
    snap = data["snapshot"]
    table = Table(
        "rank", "Δ prev", "game", "kind", "IGDB", "attention", "score", "obs. viewers*",
        title=f"Twitch top games (poll {snap['observed_at']}, {snap['games']} games)",
    )  # fmt: skip
    for g in data["items"]:
        table.add_row(
            _num(g["latest_rank"]) if g["in_latest_ranking"] else "-",
            _change(g["rank_change_since_previous_poll"]),
            escape(g["name"]),
            g["category_kind"],
            g["igdb_id"] or "-",
            g["attention_state"],
            f"{g['attention_score']:.0f}",
            _num(g["observed_viewers"])
            + (f" ({g['sample_status']})" if g["observed_viewers"] is not None else ""),
        )
    base.console.print(table)
    _footer()


# --------------------------------------------------------------------------- streams
@twitch_app.command("streams")
def twitch_streams(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    game: Annotated[str | None, typer.Option(help="Exact Twitch game name.")] = None,
    language: Annotated[str | None, typer.Option(help="ISO 639-1 code, e.g. en.")] = None,
    refresh: AutoRefresh = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Live streams from the latest stored collection (top streams, or one game)."""
    container = base._container(require_db=True)
    provider = _provider(container)
    tw = container.twitch_settings()
    game_id = None
    if game:
        _require_live(container, provider)
        found = base._run(container, lambda: _resolve(container, game))
        container = base._container(require_db=True)
        if found is None:
            container.engine.dispose()
            raise base._fail(f"Twitch has no game named exactly {game!r}", 1)
        game_id = found.id
        _provider(container)
    kind = "game_streams" if game_id else "top_streams"
    minutes = tw.cadence.high_priority_game_minutes if game_id else tw.cadence.top_streams_minutes
    if refresh or (refresh is None and _stale(container, kind, minutes, game_id)):
        _require_live(container, container.registry.get(PROVIDER))
        monitor = container.twitch_monitor(trigger="cli")
        base._run(
            container,
            (lambda: monitor.poll_game(game_id)) if game_id else monitor.poll_top_streams,
        )
    data = _read(container).streams(game_id=game_id, language=language, limit=limit)
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    snap = data["snapshot"] or {}
    table = Table(
        "#", "channel", "game", "lang", "viewers*", "live since", "title",
        title=f"Twitch live streams ({snap.get('kind')}, {snap.get('observed_at')}, "
        f"{snap.get('sample_status')} sample)",
    )  # fmt: skip
    for st in data["items"]:
        table.add_row(
            str(st["position"]),
            escape(st["user_login"]),
            escape(st["game_name"] or "-"),
            st["language"] or "-",
            _num(st["viewer_count"]),
            (st["started_at"] or "")[11:16],
            escape((st["title"] or "")[:50]),
        )
    base.console.print(table)
    base.console.print(
        f"shown: {len(data['items'])} streams, {_num(data['shown_viewer_sum'])} observed viewers; "
        f"collection: {_num(snap.get('streams'))} streams, {_num(snap.get('viewer_sum'))} viewers "
        f"({snap.get('sample_status')}: {snap.get('status_reason')})"
    )
    _footer()


# --------------------------------------------------------------------------- game
@twitch_app.command("game")
def twitch_game(
    name: Annotated[str, typer.Option("--name", help="Exact Twitch game name.")],
    hours: Annotated[int, typer.Option(min=1, max=24 * 30)] = 24,
    as_json: base.JsonOpt = False,
) -> None:
    """Resolve a game through Get Games and show its stored state, history and links."""
    container = base._container(require_db=True)
    provider = _provider(container)
    _require_live(container, provider)
    game = base._run(container, lambda: _resolve(container, name))
    container = base._container(require_db=True)
    if game is None:
        container.engine.dispose()
        raise base._fail(f"Twitch has no game named exactly {name!r}", 1)
    detail = _read(container).game_detail(game.id, hours=hours)
    container.engine.dispose()
    data = {
        "twitch": {"game_id": game.id, "name": game.name, "igdb_id": game.igdb_id},
        "verified_via": "GET /helix/games?name=",
        "detail": detail,
    }
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"[bold]{escape(game.name)}[/]  Twitch id {game.id}, IGDB id {game.igdb_id or '-'} "
        "(verified via Get Games)"
    )
    if detail:
        _print_detail(detail)
    _footer()


def _print_detail(d: dict[str, Any]) -> None:
    a = d.get("attention") or {}
    base.console.print(
        f"  rank {_num(d['latest_rank'])} (best {_num(d['best_rank'])}); observed viewers "
        f"{_num(d['observed_viewers'])} over {_num(d['observed_streams'])} streams "
        f"({d['sample_status'] or 'no game sample'})"
    )
    base.console.print(
        f"  attention [{a.get('version')}]: {d['attention_state']} (score "
        f"{d['attention_score']}); labels {', '.join(d['labels'])}"
    )
    for name, comp in (a.get("components") or {}).items():
        base.console.print(
            f"    {name}: {comp['value']} x {comp['weight']} -> {comp['contribution']}  "
            f"[dim]{escape(str(comp['basis']))}[/]"
        )
    for note in a.get("notes") or []:
        base.console.print(f"    note: {escape(note)}")
    h = d["history"]
    base.console.print(
        f"  history ({h['window_hours']} h): {len(h['ranks'])} rank observations "
        f"({h['polls_outside_top_n']} polls outside the polled top-N), "
        f"{len(h['viewers'])} viewer observations"
    )
    if d.get("monitoring"):
        m = d["monitoring"]
        base.console.print(
            f"  monitored: {m['tier']} tier ({m['origin']}), next poll {m['next_poll_at']}"
        )
    if d.get("entity"):
        e = d["entity"]
        base.console.print(
            f"  entity: {escape(e['name'])} ({e['type']}, {e['status']}"
            + (", evergreen" if e.get("evergreen") else "")
            + ")"
        )
    for ex in d.get("possible_explanations") or []:
        base.console.print(
            f"  possible explanation (co-occurrence, not cause): {escape(ex['title'][:80])}"
        )
    for k in (d.get("keyword_hypotheses") or [])[:8]:
        base.console.print(f"  PROXY_OPPORTUNITY hypothesis: {escape(k['query'])} ({k['status']})")
    eligible = [f["format"] for f in d.get("format_candidates") or [] if f["eligible"]]
    base.console.print(f"  grounded formats: {', '.join(eligible) or 'none yet'}")
    base.console.print(
        f"  Phase 5 gaps {len(d['content_gaps'])}, opportunities "
        f"{len(d['content_opportunities'])}; Phase 7 recommendations "
        f"{len(d['recommendations'])}; Phase 6 forecasts {len(d['forecasts'])}"
    )
    base.console.print(
        f"  direct YouTube search interest: {d['direct_youtube_search_interest']['detail']}"
    )


# --------------------------------------------------------------------------- monitor
@twitch_app.command("monitor")
def twitch_monitor(
    game: Annotated[str, typer.Option("--game", help="Exact Twitch game name.")],
    tier: Annotated[str, typer.Option(help="high (10 min) | low (45 min)")] = "high",
    remove: Annotated[bool, typer.Option("--remove", help="Stop monitoring the game.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Add a game to the monitored list (polled by the scheduler) and poll it once now."""
    from trend_agent.db.repositories.twitch import TwitchRepository
    from trend_agent.db.session import transaction

    if tier not in ("high", "low"):
        raise base._fail("--tier must be high or low", 2)
    container = base._container(require_db=True)
    provider = _provider(container)
    _require_live(container, provider)
    monitor = container.twitch_monitor(trigger="cli")

    async def run() -> dict[str, Any]:
        found = await monitor.resolve(game)
        if found is None:
            return {"error": f"Twitch has no game named exactly {game!r}"}
        if remove:
            with transaction(container.session_factory) as s:
                removed = TwitchRepository(s).remove_monitored(found.id)
            return {"game_id": found.id, "name": found.name, "removed": removed}
        await monitor.monitor(game, tier=tier, origin="manual")
        polled = await monitor.poll_game(found.id)
        now = container.clock()
        minutes = (
            monitor.settings.cadence.high_priority_game_minutes
            if tier == "high"
            else monitor.settings.cadence.low_priority_game_minutes
        )
        with transaction(container.session_factory) as s:
            TwitchRepository(s).mark_polled(
                found.id, now=now, next_at=now + timedelta(minutes=minutes)
            )
            monitor.assess(s, [found.id], now)
        return {"game_id": found.id, "name": found.name, "tier": tier, "polled": polled}

    out = base._run(container, run)
    container.engine.dispose()
    if "error" in out:
        raise base._fail(out["error"], 1)
    if as_json:
        base._emit_json(out)
        return
    if remove:
        base.console.print(
            f"{escape(out['name'])}: {'removed' if out['removed'] else 'was not monitored'}"
        )
        return
    p = out["polled"]
    base.console.print(
        f"Monitoring {escape(out['name'])} (id {out['game_id']}, {tier} tier). First poll: "
        f"{p['streams']} live streams, {_num(p['viewers_observed'])} observed viewers "
        f"({p['sample_status']}: {p['reason']}; {p['duplicates']} duplicates dropped)."
    )
    base.console.print(
        "[dim]The scheduler polls monitored games when TREND_AGENT_SCHEDULED_DISCOVERY=true. "
        f"{NOT_DEMAND}[/]"
    )


# --------------------------------------------------------------------------- trending
@twitch_app.command("trending")
def twitch_trending(
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
    as_json: base.JsonOpt = False,
) -> None:
    """Games whose ranking or observed viewers rose between comparable observations."""
    container = base._container(require_db=True)
    data = _read(container).trending(limit=limit)
    container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    table = Table(
        "game", "rank", "Δ positions", "viewers %/h", "comparison", "state", "score",
        title=f"Twitch attention momentum ({data['measured']} games measured)",
    )  # fmt: skip
    for g in data["items"]:
        table.add_row(
            escape(g["name"]),
            _num(g["latest_rank"]),
            _change(g["window_positions_gained"]),
            _num(g["viewers_pct_per_hour"]),
            escape(g["viewer_comparison"] or "-"),
            g["attention_state"],
            f"{g['attention_score']:.0f}",
        )
    base.console.print(table)
    if data["awaiting_comparable_observations"]:
        base.console.print(
            f"{data['awaiting_comparable_observations']} games need more comparable "
            "observations: no growth is reported for them."
        )
    _footer()


# --------------------------------------------------------------------------- history
@twitch_app.command("history")
def twitch_history(
    game: Annotated[str, typer.Option("--game", help="Twitch game name (stored or exact).")],
    hours: Annotated[int, typer.Option(min=1, max=24 * 30)] = 24,
    as_json: base.JsonOpt = False,
) -> None:
    """Stored ranking and observed-viewer history of one game (within the retention window)."""
    from trend_agent.db.repositories.twitch import TwitchRepository
    from trend_agent.db.session import transaction

    container = base._container(require_db=True)
    with transaction(container.session_factory) as s:
        row = TwitchRepository(s).game_by_name(game)
        game_id = row.game_id if row else None
    if game_id is None:
        provider = _provider(container)
        _require_live(container, provider)
        found = base._run(container, lambda: _resolve(container, game))
        container = base._container(require_db=True)
        game_id = found.id if found else None
    if game_id is None:
        container.engine.dispose()
        raise base._fail(f"no stored or Twitch game named {game!r}", 1)
    data = _read(container).history(game_id, hours=hours)
    container.engine.dispose()
    if data is None:
        raise base._fail(f"no history stored for {game!r}", 1)
    if as_json:
        base._emit_json(data)
        return
    table = Table("time (UTC)", "rank", title=f"{escape(data['name'])}: ranking ({hours} h)")
    for r in data["ranks"]:
        table.add_row(r["observed_at"][:16].replace("T", " "), str(r["rank"]))
    base.console.print(table)
    vt = Table(
        "time (UTC)", "obs. viewers*", "streams", "sample", "basis", "cap",
        title=f"{escape(data['name'])}: observed viewers",
    )  # fmt: skip
    for v in data["viewers"]:
        vt.add_row(
            v["observed_at"][:16].replace("T", " "),
            _num(v["viewers_observed"]),
            _num(v["streams_observed"]),
            v["sample_status"],
            v["sample_basis"],
            str(v["sample_cap"]),
        )
    base.console.print(vt)
    base.console.print(f"[dim]{data['note']}[/]")
    _footer()


# --------------------------------------------------------------------------- quota
@twitch_app.command("quota")
def twitch_quota(
    live: Annotated[
        bool, typer.Option("--live", help="One Helix request to read Twitch's rate-limit headers.")
    ] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Local daily request budget and (with --live) Twitch's token-bucket headers."""
    from trend_agent.providers.base import RequestContext
    from trend_agent.providers.errors import ProviderError

    container = base._container(require_db=False)
    provider = _provider(container)
    data: dict[str, Any] = {"local_budget": _usage(provider)}
    if live:
        _require_live(container, provider)

        async def ask() -> dict[str, Any]:
            try:
                await provider.client.top_games(RequestContext(), count=1)
            except ProviderError as exc:
                return {"error": exc.to_dict()}
            return {"rate_limit": provider.client.rate.as_dict()}

        data["twitch"] = base._run(container, ask)
        data["local_budget"] = _usage(provider)
    else:
        container.engine.dispose()
    if as_json:
        base._emit_json(data)
        return
    u = data["local_budget"]
    if "error" in u:
        base.console.print(f"local budget: unavailable ({escape(u['error'])})")
    else:
        base.console.print(
            f"local budget: {u['used_today']}/{u['daily_budget']} requests today, "
            f"resets {u['resets_at']}"
        )
    tw = data.get("twitch")
    if tw:
        if "error" in tw:
            base.console.print(f"[red]Twitch: {escape(tw['error']['message'])}[/]")
        else:
            r = tw["rate_limit"]
            base.console.print(
                f"Twitch bucket: {r['remaining']}/{r['limit']} points, resets {r['reset_at']}"
            )


# --------------------------------------------------------------------------- cycle / promote
@twitch_app.command("cycle")
def twitch_cycle(
    promote_games: Annotated[
        bool, typer.Option("--promote/--no-promote", help="Send emerging games to discovery.")
    ] = True,
    as_json: base.JsonOpt = False,
) -> None:
    """Run one scheduler cycle now: purge, due polls, attention, auto-monitor, promotion."""
    from trend_agent.twitch.service import promote

    container = base._container(require_db=True)
    provider = _provider(container)
    _require_live(container, provider)
    monitor = container.twitch_monitor(trigger="cli")

    async def run() -> dict[str, Any]:
        report = await monitor.run_due()
        if promote_games and report.get("candidates"):
            report["promotion"] = await promote(
                container.discovery(), monitor, container.discovery_config().config
            )
        return report

    out = base._run(container, run)
    container.engine.dispose()
    if as_json:
        base._emit_json(out)
        return
    for kind, v in out.get("polls", {}).items():
        if isinstance(v, list):
            base.console.print(f"{kind}: {len(v)} game(s) polled")
        else:
            base.console.print(f"{kind}: {v.get('sample_status')} ({v.get('reason')})")
    base.console.print(
        f"assessed {out.get('assessed')} games; auto-monitored {out.get('auto_monitored')}; "
        f"candidates {[c['name'] for c in out.get('candidates', [])]}"
    )
    for err in out.get("errors", []):
        base.console.print(f"[yellow]{escape(err)}[/]")
    if out.get("promotion"):
        base.console.print(f"promoted: {out['promotion'].get('promoted')}")


@twitch_app.command("promote")
def twitch_promote(
    game: Annotated[str, typer.Option("--game", help="Exact Twitch game name.")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Send one game to the Phase 2 pipeline (Twitch ID verified; entity resolved; keyword
    hypotheses generated where the entity qualifies)."""
    from trend_agent.twitch.service import promote

    container = base._container(require_db=True)
    provider = _provider(container)
    _require_live(container, provider)
    monitor = container.twitch_monitor(trigger="cli")

    async def run() -> dict[str, Any]:
        found = await monitor.resolve(game)
        if found is None:
            return {"error": f"Twitch has no game named exactly {game!r}"}
        return await promote(
            container.discovery(),
            monitor,
            container.discovery_config().config,
            game_ids=[found.id],
            dry_run=dry_run,
        )

    out = base._run(container, run)
    container.engine.dispose()
    if "error" in out:
        raise base._fail(out["error"], 1)
    if as_json:
        base._emit_json(out)
        return
    base.console.print(f"promoted: {out.get('promoted')}")
    run_info = out.get("run") or {}
    for src in run_info.get("sources", []):
        base.console.print(
            f"  {src['key']}: {src['status']}, {src['items_new']} new item(s) "
            + escape("; ".join(src["warnings"]))
        )
    base.console.print(
        f"  new entities {run_info.get('entities_new')}, new keyword hypotheses "
        f"{run_info.get('candidates_new')}"
    )
