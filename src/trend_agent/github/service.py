"""GitHub integration services: observation recording, connection test, read models.

Evidence classes shown to users (never mixed up):

* ``GITHUB_ACTIVITY``                 repository metadata and repeated observations
                                      (external developer activity)
* ``VERIFIED_RELEASE``                a release published on the repository's own
                                      GitHub releases page (tag, version, date verified)
* ``KEYWORD_HYPOTHESIS``              a search phrase *generated* from verified entities;
                                      never an observed search query
* ``EXTERNAL_ATTENTION``              independent non-GitHub sources mentioning the
                                      entity (official posts, Hacker News, YouTube metadata)
* ``DIRECT_YOUTUBE_SEARCH_INTEREST``  only from a live-verified direct provider; GitHub
                                      never produces it
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import DiscoverySourceRow
from trend_agent.db.github_models import GitHubReleaseRow, GitHubRepositoryRow
from trend_agent.db.monitoring_models import SeriesRow
from trend_agent.db.repositories.github import GitHubRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.models import Category, DiscoveryItem, DiscoverySource
from trend_agent.github.client import GitHubClient
from trend_agent.github.models import MEASUREMENT_DEFINITIONS, SIGNAL_LABEL
from trend_agent.github.momentum import Observation, compute_momentum
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import ProviderError
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.utils.time import Clock, utc_now

PROVIDER = "github"
EVIDENCE_CLASSES = {
    "GITHUB_ACTIVITY": "Repository metadata and repeated observations from the GitHub API "
    "(external developer activity). Not search interest.",
    "VERIFIED_RELEASE": "A release published on the repository's own GitHub releases page; "
    "tag, version and publication time come from GitHub. Notes are quoted, not summarised.",
    "KEYWORD_HYPOTHESIS": "A search phrase generated from a verified entity and templates. "
    "A hypothesis to test, never an observed search query.",
    "EXTERNAL_ATTENTION": "Independent sources outside GitHub mentioning the same entity "
    "(official announcements, Hacker News, YouTube video metadata). Reposts count once.",
    "DIRECT_YOUTUBE_SEARCH_INTEREST": "Relative interest in YouTube Search from a "
    "live-verified direct provider. GitHub data never produces this class.",
}


# ---------------------------------------------------------------------------- recording


class GitHubObservationRecorder:
    """Discovery observer: keeps structured repository/observation/release rows."""

    def __init__(self, retention_days: int | None) -> None:
        self._retention = timedelta(days=retention_days) if retention_days else None
        self._provider_ids: dict[int, int | None] = {}

    def _provider_id(self, session: Session, source_id: int) -> int | None:
        if source_id not in self._provider_ids:
            row = session.get(DiscoverySourceRow, source_id)
            self._provider_ids[source_id] = row.provider_id if row else None
        return self._provider_ids[source_id]

    def observe(
        self,
        session: Session,
        *,
        run_id: uuid.UUID,
        source: DiscoverySource,
        item: DiscoveryItem,
        item_id: int,
        source_id: int,
    ) -> None:
        if item.provider_name != PROVIDER:
            return
        meta = item.metadata.get("github")
        if not isinstance(meta, dict):
            return
        repo = GitHubRepository(session)
        expires = item.collected_at + self._retention if self._retention else None
        provider_id = self._provider_id(session, source_id)
        category = item.category_hint or source.category
        if meta.get("kind") == "repository":
            snapshot = meta["repository"]
            strategy = str(meta.get("strategy") or "unknown")
            repository_id = repo.upsert_repository(
                snapshot,
                assessment=meta.get("assessment") or {},
                category=category,
                strategy=strategy,
                source_key=source.key,
                run_id=run_id,
                item_id=item_id,
                provider_id=provider_id,
                seen_at=item.collected_at,
                expires_at=expires,
                is_synthetic=item.is_synthetic,
            )
            repo.add_observation(
                repository_id,
                snapshot,
                observed_at=item.collected_at,
                strategy=strategy,
                source_key=source.key,
                run_id=run_id,
                item_id=item_id,
                provider_id=provider_id,
                expires_at=expires,
                is_synthetic=item.is_synthetic,
            )
        elif meta.get("kind") == "release":
            repo.upsert_release(
                meta,
                category=category,
                source_key=source.key,
                run_id=run_id,
                item_id=item_id,
                provider_id=provider_id,
                seen_at=item.collected_at,
                expires_at=expires,
                is_synthetic=item.is_synthetic,
            )

    def finish(self, session: Session, *, run_id: uuid.UUID, item_ids: Sequence[int]) -> None:
        GitHubRepository(session).link_entities(item_ids)


# ---------------------------------------------------------------------------- connection


async def connection_test(
    client: GitHubClient, *, sample_query: str = "topic:llm"
) -> dict[str, Any]:
    """Bounded live test: ``GET /rate_limit`` (free) and one search (1 search request).

    Returns a report without secrets; ``error`` holds the typed error on failure.
    """
    ctx = RequestContext()
    report: dict[str, Any] = {
        "ok": False,
        "auth_mode": client.auth_mode(),
        "api_version": client.api_version,
        "search_requests": 0,
        "rate_limits": {},
        "sample": [],
        "error": None,
    }
    try:
        report["rate_limits"] = await client.rate_limit(ctx)
        report["auth_mode"] = client.auth_mode()
        since = (client._clock() - timedelta(days=7)).date()
        report["search_requests"] = 1
        result = await client.search_repositories(
            f"{sample_query} created:>={since} fork:false", ctx, per_page=3, max_pages=1
        )
        report["sample_query"] = result.query
        report["sample_total_count"] = result.total_count
        report["sample"] = [
            {
                "repository_id": r.id,
                "full_name": r.full_name,
                "html_url": r.html_url,
                "created_at": r.created_at.isoformat(),
                "stargazers_count": r.stargazers_count,
            }
            for r in result.repositories
        ]
        report["ok"] = True
    except ProviderError as exc:
        report["auth_mode"] = client.auth_mode()
        report["error"] = exc
        report["error_detail"] = exc.to_dict()
    report["rate_limit_state"] = client.rate_limit_snapshot()
    return report


# ---------------------------------------------------------------------------- read models


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def repository_view(row: GitHubRepositoryRow, now: datetime) -> dict[str, Any]:
    return {
        "repository_id": row.repository_id,
        "full_name": row.full_name,
        "name": row.name,
        "owner_login": row.owner_login,
        "html_url": row.html_url,
        "description": row.description,
        "language": row.language,
        "topics": list(row.topics or []),
        "license": row.license_spdx,
        "category": row.category.value,
        "strategies": list(row.strategies or []),
        "created_at": _iso(row.created_at_github),
        "pushed_at": _iso(row.latest_pushed_at),
        "updated_at": _iso(row.latest_updated_at),
        "stargazers_count": row.latest_stargazers_count,
        "forks_count": row.latest_forks_count,
        "archived": row.archived,
        "relevance": row.relevance,
        "discovery_score": row.discovery_score,
        "quality_flags": list(row.quality_flags or []),
        "entity_id": row.entity_id,
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "freshness_hours": round((now - row.last_seen_at).total_seconds() / 3600, 1),
        "source_key": row.first_source_key,
        "run_id": str(row.first_run_id) if row.first_run_id else None,
        "item_id": row.item_id,
        "evidence_class": "GITHUB_ACTIVITY",
        "signal_label": SIGNAL_LABEL,
        "synthetic": row.is_synthetic,
    }


def release_view(row: GitHubReleaseRow, now: datetime) -> dict[str, Any]:
    return {
        "release_id": row.release_id,
        "repository_id": row.repository_id,
        "repository": row.repository_full_name,
        "tag_name": row.tag_name,
        "name": row.name,
        "version": row.version,
        "release_level": row.release_level,
        "prerelease": row.prerelease,
        "html_url": row.html_url,
        "published_at": _iso(row.published_at_github),
        "is_breaking": row.is_breaking,
        "breaking_change_lines": list(row.breaking_lines or []),
        "new_capability_lines": list(row.capability_lines or []),
        "api_lines": list(row.api_lines or []),
        "flags": list(row.analysis_flags or []),
        "category": row.category.value,
        "entity_id": row.entity_id,
        "first_seen_at": _iso(row.first_seen_at),
        "freshness_hours": round((now - row.last_seen_at).total_seconds() / 3600, 1),
        "source_key": row.source_key,
        "item_id": row.item_id,
        "evidence_class": "VERIFIED_RELEASE",
        "basis": "tag, version and date from GitHub; notes quoted verbatim, never summarised",
        "synthetic": row.is_synthetic,
    }


def direct_interest_available(session: Session) -> bool:
    return bool(
        session.execute(
            sa.select(sa.func.count())
            .select_from(SeriesRow)
            .where(
                SeriesRow.measurement_category
                == MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                SeriesRow.is_synthetic.is_(False),
            )
        ).scalar_one()
    )


class GitHubReadService:
    """Database read models for the CLI and the dashboard API."""

    def __init__(
        self,
        factory: SessionFactory,
        *,
        clock: Clock = utc_now,
        min_interval_hours: float = 6.0,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._min_interval = min_interval_hours

    def repositories(
        self,
        *,
        days: int | None = None,
        category: Category | None = None,
        strategy: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "first_seen",
        include_synthetic: bool = False,
    ) -> dict[str, Any]:
        now = self._clock()
        since = now - timedelta(days=days) if days else None
        with transaction(self._factory) as s:
            repo = GitHubRepository(s)
            rows, total = repo.repositories(
                since=since,
                category=category,
                strategy=strategy,
                include_synthetic=include_synthetic,
                limit=limit,
                offset=offset,
                order=order,
            )
            items = [repository_view(r, now) for r in rows]
        return {"total": total, "items": items, "since": _iso(since)}

    def releases(
        self,
        *,
        days: int | None = None,
        category: Category | None = None,
        limit: int = 50,
        offset: int = 0,
        include_synthetic: bool = False,
    ) -> dict[str, Any]:
        now = self._clock()
        since = now - timedelta(days=days) if days else None
        with transaction(self._factory) as s:
            rows, total = GitHubRepository(s).releases(
                since=since,
                category=category,
                include_synthetic=include_synthetic,
                limit=limit,
                offset=offset,
            )
            items = [release_view(r, now) for r in rows]
        return {"total": total, "items": items, "since": _iso(since)}

    def repository_detail(self, ref: int | str) -> dict[str, Any] | None:
        now = self._clock()
        with transaction(self._factory) as s:
            repo = GitHubRepository(s)
            row = repo.repository(ref)
            if row is None:
                return None
            view = repository_view(row, now)
            obs = repo.observations(row.repository_id)
            latest_release = repo.latest_release_at(row.repository_id)
            corroboration = repo.corroboration(row.entity_id)
            momentum = compute_momentum(
                [
                    Observation(
                        observed_at=o.observed_at,
                        stargazers_count=o.stargazers_count,
                        forks_count=o.forks_count,
                        open_issues_count=o.open_issues_count,
                        pushed_at=o.pushed_at_github,
                    )
                    for o in obs
                ],
                now=now,
                created_at=row.created_at_github,
                latest_release_at=latest_release,
                corroborations=int(corroboration["independent_publishers"]),
                min_interval_hours=self._min_interval,
            )
            releases, _ = repo.releases(repository_id=row.repository_id, limit=10)
            entity = repo.entity(row.entity_id)
            keywords = repo.keywords_for(row.item_id, row.entity_id)
            direct = direct_interest_available(s)
            view.update(
                {
                    "entity": {
                        "id": entity.id,
                        "name": entity.canonical_name,
                        "type": entity.entity_type.value,
                        "status": entity.status.value,
                    }
                    if entity
                    else None,
                    "observations": [
                        {
                            "observed_at": o.observed_at.isoformat(),
                            "stargazers_count": o.stargazers_count,
                            "forks_count": o.forks_count,
                            "pushed_at": _iso(o.pushed_at_github),
                            "strategy": o.strategy,
                            "source_key": o.source_key,
                            "run_id": str(o.run_id) if o.run_id else None,
                        }
                        for o in obs
                    ],
                    "momentum": momentum.as_dict(),
                    "releases": [release_view(r, now) for r in releases],
                    "keyword_candidates": keywords,
                    "external_attention": {"evidence_class": "EXTERNAL_ATTENTION", **corroboration},
                    "direct_youtube_search_interest": {
                        "evidence_class": "DIRECT_YOUTUBE_SEARCH_INTEREST",
                        "available": direct,
                        "detail": "not measured: no live-verified direct YouTube Search "
                        "provider has data"
                        if not direct
                        else "see the trends page for monitored queries",
                    },
                }
            )
        return view

    def summary(self) -> dict[str, Any]:
        now = self._clock()
        with transaction(self._factory) as s:
            stats = GitHubRepository(s).stats()
        last = stats.get("last_observed_at")
        return {
            **{k: v.isoformat() if isinstance(v, datetime) else v for k, v in stats.items()},
            "freshness_hours": round((now - last).total_seconds() / 3600, 1) if last else None,
            "evidence_classes": EVIDENCE_CLASSES,
            "definitions": MEASUREMENT_DEFINITIONS,
            "signal_label": SIGNAL_LABEL,
        }
