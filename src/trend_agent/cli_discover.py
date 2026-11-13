"""``trend-agent discover ...`` commands (Phase 2: keyword discovery).

Discovery output is *candidate* keywords with traceable evidence and a monitoring
priority. Nothing printed here is search volume or demand.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import sqlalchemy as sa
import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoverySourceRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.repositories.discovery import DiscoveryRepository
from trend_agent.db.session import transaction
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.demo import run_demo
from trend_agent.discovery.models import TARGET_CATEGORIES, CandidateStatus, Category
from trend_agent.discovery.service import DiscoveryRunSummary
from trend_agent.utils.text import QueryValidationError, canonicalize_query

discover_app = typer.Typer(
    help="Keyword discovery: candidate keywords with evidence (never search volume).",
    no_args_is_help=True,
)

NOT_DEMAND = "Priority ranks candidates for monitoring; it is not search volume or demand."


def _service(require_db: bool = True) -> tuple[Any, Any]:
    container = base._container(require_db=require_db)
    try:
        return container, container.discovery()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None


def _categories(category: list[Category] | None, all_: bool) -> list[Category]:
    if all_ or not category:
        if not all_ and not category:
            raise base._fail("choose --category (ai, technology, gaming) or --all", 2)
        return list(TARGET_CATEGORIES)
    bad = [c for c in category if c not in TARGET_CATEGORIES]
    if bad:
        raise base._fail(f"not a discovery category: {', '.join(c.value for c in bad)}", 2)
    return list(category)


def _keyword_rows(
    container: Any,
    category: Category | None,
    statuses: list[CandidateStatus] | None,
    include_synthetic: bool,
    limit: int,
) -> list[dict[str, Any]]:
    with transaction(container.session_factory) as session:
        repo = DiscoveryRepository(session)
        rows = repo.keywords(
            category=category, statuses=statuses, include_synthetic=include_synthetic, limit=limit
        )
        breakdown = repo.evidence_breakdown([k.id for k, _q, _e in rows])
        return [
            {
                "query": q.original_query,
                "canonical": q.canonical_query,
                "category": k.primary_category.value,
                "intent": k.primary_intent.value,
                "origin": k.origin.value,
                "method": k.discovery_type.value,
                "template": k.template_id,
                "status": k.candidate_status.value,
                "priority": k.priority_score,
                "novelty": k.novelty_score,
                "novelty_kind": k.novelty_kind.value,
                "independent_origins": (k.score_components or {}).get("independent_origins"),
                "entity": e.canonical_name if e else None,
                "evidence": breakdown.get(k.id, {}),
                "synthetic": k.is_synthetic,
                "scoring_version": k.scoring_version,
            }
            for k, q, e in rows
        ]


def _print_keywords(rows: list[dict[str, Any]], title: str) -> None:
    table = Table(
        "prio", "query", "intent", "origin", "status", "novelty", "orig", "evidence", title=title
    )
    for r in rows:
        evidence = ", ".join(f"{k}:{v}" for k, v in sorted(r["evidence"].items()))
        table.add_row(
            f"{r['priority']:.1f}",
            escape(r["query"]) + (" [dim](synthetic)[/]" if r["synthetic"] else ""),
            r["intent"],
            r["origin"].replace("_", " "),
            r["status"],
            r["novelty_kind"],
            str(r["independent_origins"] or 0),
            escape(evidence),
        )
    base.console.print(table)


def _print_run(summary: DiscoveryRunSummary, label: str = "") -> None:
    colour = {"succeeded": "green", "partial": "yellow"}.get(summary.status.value, "red")
    base.console.print(
        f"[bold]{label}Discovery run[/] status=[{colour}]{summary.status.value}[/]"
        f"{' (dry run)' if summary.dry_run else ''}{' [SYNTHETIC]' if summary.is_synthetic else ''}"
    )
    table = Table(
        "category",
        "slots",
        "explore",
        "unused",
        "sources",
        "failed",
        "items",
        "new",
        "dup",
        "entities+",
        "candidates",
        "new kw",
        "merged",
        "rejected",
        "prioritized",
        "quota",
    )
    for cat, s in summary.categories.items():
        table.add_row(
            cat,
            str(s.slots),
            str(s.exploration_slots),
            str(s.unused_slots),
            str(s.sources_checked),
            str(s.sources_failed),
            str(s.items_received),
            str(s.items_new),
            str(s.items_duplicate),
            str(s.entities_new),
            str(s.candidates_total),
            str(s.candidates_new),
            str(s.candidates_merged),
            str(s.candidates_rejected),
            str(s.prioritized),
            ", ".join(f"{k}={v}" for k, v in s.quota_used.items()) or "-",
        )
    base.console.print(table)
    for outcome in summary.sources:
        if outcome.status in {"failed"}:
            base.console.print(
                f"[yellow]source {escape(outcome.key)}: {outcome.error_type}[/] "
                f"{escape(outcome.error_message or '')}"
            )
    skipped: dict[str, int] = {}
    for skip in summary.skipped:
        reason = skip["reason"].split(";")[0][:90]
        skipped[reason] = skipped.get(reason, 0) + 1
    for reason, count in sorted(skipped.items(), key=lambda kv: -kv[1]):
        base.console.print(f"[dim]skipped {count} source(s): {escape(reason)}[/]")
    if summary.dormant:
        base.console.print(f"{summary.dormant} keyword(s) moved to dormant.")


# --------------------------------------------------------------------------- run
@discover_app.command("run")
def discover_run(
    category: Annotated[list[Category] | None, typer.Option("--category", "-c")] = None,
    all_: Annotated[bool, typer.Option("--all", help="All categories within budgets.")] = False,
    max_fetches: Annotated[int | None, typer.Option(min=1, max=1000)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only; fetch nothing.")] = False,
    force: Annotated[bool, typer.Option(help="Ignore minimum polling intervals.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Discover candidates for the chosen categories (budget-, quota- and interval-aware)."""
    cats = _categories(category, all_)
    container, service = _service()
    summary = base._run(
        container,
        lambda: service.run(cats, max_fetches=max_fetches, dry_run=dry_run, force=force),
    )
    if as_json:
        base._emit_json(summary.model_dump(mode="json"))
        return
    _print_run(summary)
    if not dry_run:
        fresh = base.CONTAINER_FACTORY(base.SETTINGS_FACTORY())
        for cat in cats:
            rows = _keyword_rows(fresh, cat, [CandidateStatus.PRIORITIZED], False, 10)
            if rows:
                _print_keywords(rows, f"Top {cat.value} candidates")
        fresh.engine.dispose()
        base.console.print(f"[dim]{NOT_DEMAND}[/]")
    if summary.status.value == "failed":
        raise typer.Exit(1)


# --------------------------------------------------------------------------- demo
@discover_app.command("demo")
def discover_demo(
    limit: Annotated[int, typer.Option(min=1, max=50, help="Keywords shown per category.")] = 8,
    as_json: base.JsonOpt = False,
) -> None:
    """Run the full pipeline on the bundled SYNTHETIC fixture (no network, no quota)."""
    container = base._container(require_db=True)
    try:
        container.discovery_config()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"discovery configuration: {exc}", 2) from None
    results = base._run(container, lambda: run_demo(container))
    fresh = base.CONTAINER_FACTORY(base.SETTINGS_FACTORY())
    lists = {
        cat.value: _keyword_rows(
            fresh, cat, [CandidateStatus.PRIORITIZED, CandidateStatus.VALIDATED], True, limit
        )
        for cat in TARGET_CATEGORIES
    }
    fresh.engine.dispose()
    if as_json:
        base._emit_json(
            {"stages": {name: s.model_dump(mode="json") for name, s in results}, "keywords": lists}
        )
        return
    base.console.print(
        "[bold yellow]SYNTHETIC DEMO - fictional entities; not real topics or demand.[/]"
    )
    for name, summary in results:
        _print_run(summary, f"[{name}] ")
    for cat, rows in lists.items():
        _print_keywords(rows, f"{cat}: top synthetic candidates")
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- expand
@discover_app.command("expand")
def discover_expand(
    entity: Annotated[str, typer.Option("--entity", "-e", help="Stored entity name or alias.")],
    as_json: base.JsonOpt = False,
) -> None:
    """Show grounded search hypotheses for a stored entity (and why templates were skipped)."""
    container, service = _service()
    previews = service.expand_preview(entity)
    base._run(container, base._noop)
    data: list[dict[str, Any]] = [
        {
            "entity": row.canonical_name,
            "type": row.entity_type.value,
            "category": row.category.value,
            "status": row.status.value,
            "attributes": list(row.attributes or []),
            "hypotheses": [
                {"query": c.query, "template": c.template_id, "grounding": list(c.grounding)}
                for c in cands
            ],
            "skipped": [{"template": t, "reason": r} for t, r in skipped],
        }
        for row, cands, skipped in previews
    ]
    if as_json:
        base._emit_json(data)
        return
    if not data:
        raise base._fail(f"no stored entity matches {entity!r}; run discovery first", 1)
    for d in data:
        base.console.print(
            f"[bold]{escape(d['entity'])}[/] ({d['type']}, {d['category']}, {d['status']}) "
            f"grounded attributes: {', '.join(d['attributes']) or 'none'}"
        )
        for h in d["hypotheses"]:
            base.console.print(f"  + {escape(h['query'])}  [dim]{h['template']}[/]")
        for s in d["skipped"]:
            base.console.print(f"  [dim]- {s['template']}: {escape(s['reason'])}[/]")
    base.console.print(
        "[dim]Generated hypotheses are not evidence that anyone searched for them.[/]"
    )


# --------------------------------------------------------------------------- keywords
@discover_app.command("keywords")
def discover_keywords(
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    status: Annotated[list[CandidateStatus] | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    include_synthetic: Annotated[bool, typer.Option("--include-synthetic")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """List candidates by monitoring priority (default: validated + prioritized, real data)."""
    container = base._container(require_db=True)
    statuses = status or [CandidateStatus.PRIORITIZED, CandidateStatus.VALIDATED]
    rows = _keyword_rows(container, category, statuses, include_synthetic, limit)
    base._run(container, base._noop)
    if as_json:
        base._emit_json(rows)
        return
    if not rows:
        hint = "" if include_synthetic else " (add --include-synthetic to see demo data)"
        base.console.print(f"No candidates found{hint}.")
        return
    _print_keywords(rows, f"Candidates{' - ' + category.value if category else ''}")
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


@discover_app.command("show")
def discover_show(
    query: Annotated[str, typer.Option("--query", "-q")],
    as_json: base.JsonOpt = False,
) -> None:
    """Full provenance of one candidate: evidence, scores, cluster decisions, history."""
    try:
        canonical = canonicalize_query(query)
    except QueryValidationError as exc:
        raise base._fail(str(exc)) from None
    container = base._container(require_db=True)
    with transaction(container.session_factory) as session:
        repo = DiscoveryRepository(session)
        q = (
            session.execute(
                sa.select(SearchQueryRow).where(SearchQueryRow.canonical_query == canonical)
            )
            .scalars()
            .first()
        )
        keyword = repo.keyword_by_query(q.id) if q else None
        if keyword is None:
            container.engine.dispose()
            raise base._fail(f"no discovered keyword {canonical!r}")
        entity = session.get(DiscoveredEntityRow, keyword.entity_id) if keyword.entity_id else None
        data: dict[str, Any] = {
            "query": q.original_query if q else query,
            "origin": keyword.origin.value,
            "method": keyword.discovery_type.value,
            "template": keyword.template_id,
            "status": keyword.candidate_status.value,
            "status_reason": keyword.status_reason,
            "category": keyword.primary_category.value,
            "category_scores": keyword.category_scores,
            "intent": keyword.primary_intent.value,
            "intent_scores_uncalibrated": keyword.intent_scores,
            "entity": entity.canonical_name if entity else None,
            "novelty": {
                "score": keyword.novelty_score,
                "kind": keyword.novelty_kind.value,
                "components": keyword.novelty_components,
            },
            "priority": keyword.score_components,
            "topic_assignment": keyword.topic_assignment,
            "intent_assignment": keyword.intent_assignment,
            "grounding": list(keyword.grounding or []),
            "synthetic": keyword.is_synthetic,
            "evidence": [
                {
                    "provider": ev.provider_name,
                    "type": ev.evidence_type.value,
                    "relation": ev.relation.value,
                    "quality": ev.evidence_quality,
                    "source_reference": ev.source_reference,
                    "published_at": ev.source_published_at,
                    "collected_at": ev.collected_at,
                }
                for ev in repo.evidence_rows(keyword.id)
            ],
            "history": [
                {
                    "from": h.from_status.value if h.from_status else None,
                    "to": h.to_status.value,
                    "reason": h.reason,
                    "actor": h.actor,
                    "at": h.changed_at,
                }
                for h in repo.history(keyword.id)
            ],
        }
    base._run(container, base._noop)
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"[bold]{escape(str(data['query']))}[/]  {data['status']} "
        f"({escape(str(data['status_reason']))})"
    )
    base.console.print(
        f"origin={data['origin']} method={data['method']} template={data['template']} "
        f"category={data['category']} intent={data['intent']} entity={escape(str(data['entity']))}"
    )
    prio = data["priority"] or {}
    base.console.print(
        f"priority={prio.get('score')} ({prio.get('version')}) components={prio.get('components')}"
    )
    base.console.print(f"contributions={prio.get('contributions')}")
    base.console.print(f"novelty={data['novelty']}")
    base.console.print(f"topic: {data['topic_assignment']}")
    base.console.print(f"intent cluster: {data['intent_assignment']}")
    table = Table("type", "relation", "quality", "published", "source", title="Evidence")
    for ev in data["evidence"]:
        table.add_row(
            ev["type"],
            ev["relation"],
            f"{ev['quality']:.0f}",
            base._fmt(ev["published_at"]),
            escape(ev["source_reference"]),
        )
    base.console.print(table)
    for h in data["history"]:
        base.console.print(
            f"  {base._fmt(h['at'])} {h['from']} -> {h['to']}: {escape(h['reason'])}"
        )
    base.console.print(f"[dim]{NOT_DEMAND}[/]")


# --------------------------------------------------------------------------- entities
@discover_app.command("entities")
def discover_entities(
    recent: Annotated[
        bool, typer.Option("--recent", help="Only entities first seen recently.")
    ] = False,
    days: Annotated[int, typer.Option(min=1, max=3650)] = 7,
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    include_synthetic: Annotated[bool, typer.Option("--include-synthetic")] = False,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    as_json: base.JsonOpt = False,
) -> None:
    """Discovered entities (new releases, events, items, products) with status and lineage."""
    container = base._container(require_db=True)
    since = container.clock() - timedelta(days=days if recent else 36500)
    with transaction(container.session_factory) as session:
        rows = DiscoveryRepository(session).recent_entities(
            since, category, include_synthetic, limit
        )
        parents = {
            r.id: session.get(DiscoveredEntityRow, r.parent_entity_id).canonical_name  # type: ignore[union-attr]
            for r in rows
            if r.parent_entity_id
        }
        data: list[dict[str, Any]] = [
            {
                "name": r.canonical_name,
                "type": r.entity_type.value,
                "category": r.category.value,
                "status": r.status.value,
                "parent": parents.get(r.id),
                "version": r.version,
                "release_level": r.release_level,
                "first_seen": r.first_seen_at,
                "release_date": r.release_date,
                "release_date_basis": (r.extra or {}).get("release_date_basis"),
                "attributes": list(r.attributes or []),
                "independent_origins": (r.extra or {}).get("independent_origins"),
                "conflicting_release_dates": (r.extra or {}).get("conflicting_release_dates"),
                "curated": r.evergreen,
                "family": r.is_family,
                "synthetic": r.is_synthetic,
            }
            for r in rows
        ]
    base._run(container, base._noop)
    if as_json:
        base._emit_json(data)
        return
    if not data:
        base.console.print(
            "No entities found" + ("" if include_synthetic else " (try --include-synthetic)") + "."
        )
        return
    table = Table(
        "name",
        "type",
        "cat",
        "status",
        "parent",
        "first seen",
        "release",
        "origins",
        "attributes",
        title="Discovered entities",
    )
    for d in data:
        flags = " (curated)" if d["curated"] else " (family)" if d["family"] else ""
        release = base._fmt(d["release_date"])
        if d["conflicting_release_dates"]:
            release += " [yellow](sources disagree)[/]"
        table.add_row(
            escape(d["name"]) + flags + (" [dim](synthetic)[/]" if d["synthetic"] else ""),
            d["type"],
            d["category"],
            d["status"],
            escape(d["parent"] or "-"),
            base._fmt(d["first_seen"]),
            release,
            str(d["independent_origins"] or "-"),
            ", ".join(d["attributes"]),
        )
    base.console.print(table)


# --------------------------------------------------------------------------- sources
@discover_app.command("sources")
def discover_sources(
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    as_json: base.JsonOpt = False,
) -> None:
    """Provider readiness and per-source health (no network calls)."""
    container, service = _service(require_db=False)
    reports = [
        p.status()
        for p in container.registry.all()
        if p.descriptor.name in {s.provider for s in service.configured_sources()}
    ]
    configured = [
        s for s in service.configured_sources() if category is None or s.category is category
    ]
    db_state: dict[str, DiscoverySourceRow] = {}
    try:
        with transaction(container.session_factory) as session:
            db_state = {r.key: r for r in DiscoveryRepository(session).sources()}
    except Exception:  # noqa: S110 - database optional for this view
        pass
    rows: list[dict[str, Any]] = []
    for s in configured:
        ok, why, _health = service.readiness(s)
        state = db_state.get(s.key)
        rows.append(
            {
                "key": s.key,
                "provider": s.provider,
                "category": s.category.value,
                "type": s.source_type.value,
                "enabled": s.enabled,
                "verified": s.verified,
                "ready": ok,
                "not_ready_reason": why or None,
                "health": state.health_status.value if state else "unknown",
                "last_checked": state.last_checked_at if state else None,
                "last_success": state.last_success_at if state else None,
                "failures": state.consecutive_failures if state else 0,
                "expected_yield": state.expected_yield if state else 0.0,
                "last_error": state.last_error if state else None,
            }
        )
    base._run(container, base._noop)
    if as_json:
        base._emit_json(
            {"providers": [r.model_dump(mode="json") for r in reports], "sources": rows}
        )
        return
    pt = Table("provider", "status", "detail", title="Discovery providers")
    for r in reports:
        pt.add_row(r.provider_name, r.status.value, escape(r.detail))
    base.console.print(pt)
    st = Table(
        "source",
        "cat",
        "type",
        "ready",
        "health",
        "last checked",
        "fail",
        "yield",
        title=f"Sources ({len(rows)})",
    )
    for r in rows:
        st.add_row(
            escape(r["key"]),
            r["category"],
            r["type"],
            "yes" if r["ready"] else f"no: {escape((r['not_ready_reason'] or '')[:40])}",
            r["health"],
            base._fmt(r["last_checked"]),
            str(r["failures"]),
            f"{r['expected_yield']:.1f}",
        )
    base.console.print(st)


# --------------------------------------------------------------------------- clusters
@discover_app.command("clusters")
def discover_clusters(
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="Topics shown.")] = 20,
    include_synthetic: Annotated[bool, typer.Option("--include-synthetic")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Topic clusters and their intent clusters, with members and merge methods."""
    container = base._container(require_db=True)
    with transaction(container.session_factory) as session:
        from trend_agent.db.discovery_models import IntentClusterRow, TopicClusterRow

        stmt = (
            sa.select(TopicClusterRow, IntentClusterRow, DiscoveredKeywordRow, SearchQueryRow)
            .join(DiscoveredKeywordRow, DiscoveredKeywordRow.topic_cluster_id == TopicClusterRow.id)
            .join(IntentClusterRow, IntentClusterRow.id == DiscoveredKeywordRow.intent_cluster_id)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .where(DiscoveredKeywordRow.candidate_status != CandidateStatus.REJECTED)
        )
        if category is not None:
            stmt = stmt.where(TopicClusterRow.category == category)
        if not include_synthetic:
            stmt = stmt.where(DiscoveredKeywordRow.is_synthetic.is_(False))
        topics: dict[str, dict[str, Any]] = {}
        for t, i, k, q in session.execute(
            stmt.order_by(TopicClusterRow.id, IntentClusterRow.id)
        ).all():
            topic = topics.setdefault(
                t.canonical_topic, {"category": t.category.value, "intents": {}, "best": 0.0}
            )
            topic["best"] = max(topic["best"], k.priority_score)
            cluster = topic["intents"].setdefault(
                i.cluster_key, {"intent": i.canonical_intent.value, "members": []}
            )
            cluster["members"].append(
                {
                    "query": q.original_query,
                    "merge": (k.intent_assignment or {}).get("method"),
                    "score": (k.intent_assignment or {}).get("score"),
                }
            )
    base._run(container, base._noop)
    ordered = sorted(topics.items(), key=lambda kv: -kv[1]["best"])[:limit]
    if as_json:
        base._emit_json(dict(ordered))
        return
    if not ordered:
        base.console.print(
            "No clusters" + ("" if include_synthetic else " (try --include-synthetic)") + "."
        )
        return
    for name, topic in ordered:
        base.console.print(
            f"[bold]{escape(name)}[/] ({topic['category']}, best priority {topic['best']:.1f})"
        )
        for key, cluster in topic["intents"].items():
            members = "; ".join(
                f"{m['query']}" + (f" [{m['merge']} {m['score']}]" if m["merge"] != "new" else "")
                for m in cluster["members"]
            )
            base.console.print(f"  {cluster['intent']:<17} {escape(key):<32} {escape(members)}")


# --------------------------------------------------------------------------- stats
@discover_app.command("stats")
def discover_stats(
    days: Annotated[
        int, typer.Option(min=1, max=3650, help="Window for 'new'/'recent' counts.")
    ] = 1,
    as_json: base.JsonOpt = False,
) -> None:
    """Totals, today's discoveries, provider failures, category mix, provenance."""
    container = base._container(require_db=True)
    since = container.clock() - timedelta(days=days)
    with transaction(container.session_factory) as session:
        stats = DiscoveryRepository(session).stats(since)
    base._run(container, base._noop)
    if as_json:
        base._emit_json({"window_days": days, **stats})
        return
    table = Table("metric", "value", title=f"Discovery statistics (window: {days} day(s))")
    labels = {
        "total_keywords": "total discovered keywords",
        "new_keywords_since": "new keywords in window",
        "new_entities_since": "new entities in window",
        "total_entities": "total entities",
        "items_total": "stored source items",
        "sources_checked_since": "sources checked in window",
        "candidates_accepted_since": "candidates accepted in window",
        "duplicates_merged_since": "duplicates merged in window",
        "candidates_rejected_since": "candidates rejected in window",
        "provider_failures_since": "provider failures in window",
    }
    for key, label in labels.items():
        table.add_row(label, str(stats[key]))
    for key in ("by_category", "by_status", "by_origin", "provenance_by_evidence_type"):
        table.add_row(
            key.replace("_", " "),
            ", ".join(f"{k}={v}" for k, v in sorted(stats[key].items())) or "-",
        )
    base.console.print(table)
