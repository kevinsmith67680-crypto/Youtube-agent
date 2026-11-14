"""Persistence for GitHub repositories, repeated observations and releases.

Every write is idempotent: repositories are keyed by GitHub's stable repository ID,
observations by (repository, collection time), releases by GitHub's release ID.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    EntityMentionRow,
)
from trend_agent.db.github_models import (
    GitHubObservationRow,
    GitHubReleaseRow,
    GitHubRepositoryRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.discovery.models import Category, EntityStatus
from trend_agent.utils.time import parse_rfc3339


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return parse_rfc3339(value)
    return None


class GitHubRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ writes
    def upsert_repository(
        self,
        snapshot: dict[str, Any],
        *,
        assessment: dict[str, Any],
        category: Category,
        strategy: str,
        source_key: str,
        run_id: uuid.UUID | None,
        item_id: int | None,
        provider_id: int | None,
        seen_at: datetime,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> int:
        values = {
            "repository_id": int(snapshot["repository_id"]),
            "full_name": snapshot["full_name"],
            "name": snapshot["name"],
            "owner_login": snapshot["owner_login"],
            "owner_type": snapshot.get("owner_type"),
            "html_url": snapshot["html_url"],
            "description": snapshot.get("description"),
            "language": snapshot.get("language"),
            "topics": list(snapshot.get("topics") or [])[:30],
            "license_spdx": snapshot.get("license"),
            "created_at_github": _dt(snapshot["created_at"]),
            "latest_pushed_at": _dt(snapshot.get("pushed_at")),
            "latest_updated_at": _dt(snapshot.get("updated_at")),
            "latest_stargazers_count": int(snapshot["stargazers_count"]),
            "latest_forks_count": int(snapshot["forks_count"]),
            "size_kb": int(snapshot.get("size_kb") or 0),
            "archived": bool(snapshot.get("archived")),
            "is_fork": bool(snapshot.get("fork")),
            "category": category,
            "strategies": [strategy],
            "relevance": float(assessment.get("relevance", 0.0)),
            "discovery_score": float(assessment.get("discovery_score", 0.0)),
            "quality_flags": list(assessment.get("flags") or []),
            "entity_type": assessment.get("entity_type"),
            "item_id": item_id,
            "provider_id": provider_id,
            "first_source_key": source_key,
            "first_run_id": run_id,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "is_synthetic": is_synthetic,
            "expires_at": expires_at,
        }
        t = GitHubRepositoryRow.__table__
        stmt = insert(GitHubRepositoryRow).values(**values)
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["repository_id"],
            set_={
                # Renames and transfers keep the ID; the latest record wins.
                "full_name": ex.full_name,
                "name": ex.name,
                "owner_login": ex.owner_login,
                "owner_type": ex.owner_type,
                "html_url": ex.html_url,
                "description": ex.description,
                "language": ex.language,
                "topics": ex.topics,
                "license_spdx": ex.license_spdx,
                "latest_pushed_at": ex.latest_pushed_at,
                "latest_updated_at": ex.latest_updated_at,
                "latest_stargazers_count": sa.case(
                    (ex.last_seen_at >= t.c.last_seen_at, ex.latest_stargazers_count),
                    else_=t.c.latest_stargazers_count,
                ),
                "latest_forks_count": sa.case(
                    (ex.last_seen_at >= t.c.last_seen_at, ex.latest_forks_count),
                    else_=t.c.latest_forks_count,
                ),
                "size_kb": ex.size_kb,
                "archived": ex.archived,
                "is_fork": ex.is_fork,
                "strategies": sa.case(
                    (t.c.strategies.contains(ex.strategies), t.c.strategies),
                    else_=sa.func.array_cat(t.c.strategies, ex.strategies),
                ),
                "relevance": ex.relevance,
                "discovery_score": ex.discovery_score,
                "quality_flags": ex.quality_flags,
                "entity_type": sa.func.coalesce(t.c.entity_type, ex.entity_type),
                "item_id": sa.func.coalesce(t.c.item_id, ex.item_id),
                "last_seen_at": sa.func.greatest(t.c.last_seen_at, ex.last_seen_at),
                "expires_at": sa.func.greatest(t.c.expires_at, ex.expires_at),
                "updated_at": sa.func.now(),
            },
        )
        self._s.execute(stmt)
        return int(snapshot["repository_id"])

    def add_observation(
        self,
        repository_id: int,
        snapshot: dict[str, Any],
        *,
        observed_at: datetime,
        strategy: str,
        source_key: str,
        run_id: uuid.UUID | None,
        item_id: int | None,
        provider_id: int | None,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> bool:
        stmt = (
            insert(GitHubObservationRow)
            .values(
                repository_id=repository_id,
                observed_at=observed_at,
                stargazers_count=int(snapshot["stargazers_count"]),
                forks_count=int(snapshot["forks_count"]),
                open_issues_count=int(snapshot.get("open_issues_count") or 0),
                pushed_at_github=_dt(snapshot.get("pushed_at")),
                updated_at_github=_dt(snapshot.get("updated_at")),
                size_kb=int(snapshot.get("size_kb") or 0),
                archived=bool(snapshot.get("archived")),
                strategy=strategy,
                source_key=source_key,
                run_id=run_id,
                item_id=item_id,
                provider_id=provider_id,
                collection_status="collected",
                is_synthetic=is_synthetic,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["repository_id", "observed_at"])
            .returning(GitHubObservationRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def upsert_release(
        self,
        release: dict[str, Any],
        *,
        category: Category,
        source_key: str,
        run_id: uuid.UUID | None,
        item_id: int | None,
        provider_id: int | None,
        seen_at: datetime,
        expires_at: datetime | None,
        is_synthetic: bool = False,
    ) -> bool:
        analysis = release.get("analysis") or {}
        repo_id = release.get("repository_id")
        if repo_id is not None and self._s.get(GitHubRepositoryRow, int(repo_id)) is None:
            repo_id = None  # metadata not stored (yet): keep the release, link later
        values = {
            "release_id": int(release["release_id"]),
            "repository_id": repo_id,
            "repository_full_name": release["repository"],
            "tag_name": release["tag_name"],
            "name": release.get("name"),
            "version": analysis.get("version"),
            "release_level": analysis.get("release_level") or "unknown",
            "prerelease": bool(analysis.get("release_level") == "prerelease"),
            "html_url": release["html_url"],
            "published_at_github": _dt(release.get("published_at")),
            "created_at_github": _dt(release.get("created_at")),
            "is_breaking": bool(analysis.get("breaking_change_lines")),
            "breaking_lines": list(analysis.get("breaking_change_lines") or []),
            "capability_lines": list(analysis.get("new_capability_lines") or []),
            "api_lines": list(analysis.get("api_lines") or []),
            "analysis_flags": list(analysis.get("flags") or []),
            "category": category,
            "item_id": item_id,
            "provider_id": provider_id,
            "source_key": source_key,
            "run_id": run_id,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "is_synthetic": is_synthetic,
            "expires_at": expires_at,
        }
        t = GitHubReleaseRow.__table__
        stmt = insert(GitHubReleaseRow).values(**values)
        ex = stmt.excluded
        upsert: Any = stmt.on_conflict_do_update(
            index_elements=["release_id"],
            set_={
                # Release notes can be edited after publication: keep the latest text.
                "name": ex.name,
                "repository_id": sa.func.coalesce(t.c.repository_id, ex.repository_id),
                "breaking_lines": ex.breaking_lines,
                "capability_lines": ex.capability_lines,
                "api_lines": ex.api_lines,
                "is_breaking": ex.is_breaking,
                "analysis_flags": ex.analysis_flags,
                "item_id": sa.func.coalesce(t.c.item_id, ex.item_id),
                "last_seen_at": sa.func.greatest(t.c.last_seen_at, ex.last_seen_at),
                "expires_at": sa.func.greatest(t.c.expires_at, ex.expires_at),
            },
        ).returning(sa.literal_column("xmax = 0").label("inserted"))
        return bool(self._s.execute(upsert).scalar_one())

    def link_entities(self, item_ids: Iterable[int]) -> int:
        """Attach the entity each repository/release item is *about*: the mention from the
        item's own hint (the repository / release), not an entity it merely mentions."""
        ids = sorted(set(item_ids))
        if not ids:
            return 0
        best = (
            sa.select(EntityMentionRow.item_id, EntityMentionRow.entity_id)
            .where(
                EntityMentionRow.item_id.in_(ids),
                EntityMentionRow.status != EntityStatus.REJECTED,
            )
            .order_by(
                EntityMentionRow.item_id,
                (EntityMentionRow.extractor == "source_hint").desc(),
                EntityMentionRow.confidence.desc(),
                EntityMentionRow.id,
            )
            .distinct(EntityMentionRow.item_id)
            .subquery()
        )
        updated = 0
        for model in (GitHubRepositoryRow, GitHubReleaseRow):
            res = self._s.execute(
                sa.update(model)
                .where(
                    model.item_id == best.c.item_id,
                    model.entity_id.is_distinct_from(best.c.entity_id),
                )
                .values(entity_id=best.c.entity_id)
            )
            updated += int(getattr(res, "rowcount", 0) or 0)
        # Releases first seen before their repository's metadata: link the repository now.
        self._s.execute(
            sa.update(GitHubReleaseRow)
            .where(
                GitHubReleaseRow.repository_id.is_(None),
                GitHubReleaseRow.repository_full_name == GitHubRepositoryRow.full_name,
            )
            .values(repository_id=GitHubRepositoryRow.repository_id)
        )
        return updated

    # ------------------------------------------------------------------ reads
    def repositories(
        self,
        *,
        since: datetime | None = None,
        category: Category | None = None,
        strategy: str | None = None,
        include_synthetic: bool = False,
        limit: int = 50,
        offset: int = 0,
        order: str = "first_seen",
    ) -> tuple[list[GitHubRepositoryRow], int]:
        q = sa.select(GitHubRepositoryRow)
        if since is not None:
            q = q.where(GitHubRepositoryRow.first_seen_at >= since)
        if category is not None:
            q = q.where(GitHubRepositoryRow.category == category)
        if strategy is not None:
            q = q.where(GitHubRepositoryRow.strategies.contains([strategy]))
        if not include_synthetic:
            q = q.where(GitHubRepositoryRow.is_synthetic.is_(False))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(q.subquery())).scalar_one()
        )
        key: Any = {
            "first_seen": GitHubRepositoryRow.first_seen_at.desc(),
            "score": GitHubRepositoryRow.discovery_score.desc(),
            "created": GitHubRepositoryRow.created_at_github.desc(),
        }[order]
        rows = (
            self._s.execute(
                q.order_by(key, GitHubRepositoryRow.repository_id).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def repository(self, ref: int | str) -> GitHubRepositoryRow | None:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return self._s.get(GitHubRepositoryRow, int(ref))
        return (
            self._s.execute(
                sa.select(GitHubRepositoryRow)
                .where(sa.func.lower(GitHubRepositoryRow.full_name) == str(ref).lower())
                .order_by(GitHubRepositoryRow.last_seen_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    def observations(self, repository_id: int, limit: int = 60) -> list[GitHubObservationRow]:
        rows = (
            self._s.execute(
                sa.select(GitHubObservationRow)
                .where(GitHubObservationRow.repository_id == repository_id)
                .order_by(GitHubObservationRow.observed_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    def releases(
        self,
        *,
        since: datetime | None = None,
        repository_id: int | None = None,
        category: Category | None = None,
        include_synthetic: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[GitHubReleaseRow], int]:
        q = sa.select(GitHubReleaseRow)
        if since is not None:
            q = q.where(GitHubReleaseRow.published_at_github >= since)
        if repository_id is not None:
            q = q.where(GitHubReleaseRow.repository_id == repository_id)
        if category is not None:
            q = q.where(GitHubReleaseRow.category == category)
        if not include_synthetic:
            q = q.where(GitHubReleaseRow.is_synthetic.is_(False))
        total = int(
            self._s.execute(sa.select(sa.func.count()).select_from(q.subquery())).scalar_one()
        )
        rows = (
            self._s.execute(
                q.order_by(
                    GitHubReleaseRow.published_at_github.desc().nulls_last(),
                    GitHubReleaseRow.release_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def latest_release_at(self, repository_id: int) -> datetime | None:
        return self._s.execute(
            sa.select(sa.func.max(GitHubReleaseRow.published_at_github)).where(
                GitHubReleaseRow.repository_id == repository_id,
                GitHubReleaseRow.release_level != "prerelease",
            )
        ).scalar_one()

    def keywords_for(self, item_id: int | None, entity_id: int | None) -> list[dict[str, Any]]:
        """Keyword hypotheses grounded in this repository's item or entity."""
        if item_id is None and entity_id is None:
            return []
        conds = []
        if entity_id is not None:
            conds.append(DiscoveredKeywordRow.entity_id == entity_id)
        if item_id is not None:
            conds.append(
                DiscoveredKeywordRow.id.in_(
                    sa.select(DiscoveryEvidenceRow.keyword_id).where(
                        DiscoveryEvidenceRow.item_id == item_id
                    )
                )
            )
        rows = self._s.execute(
            sa.select(DiscoveredKeywordRow, SearchQueryRow.canonical_query)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .where(sa.or_(*conds))
            .order_by(DiscoveredKeywordRow.priority_score.desc())
            .limit(25)
        ).all()
        return [
            {
                "keyword_id": k.id,
                "query": q,
                "origin": k.origin.value,
                "method": k.discovery_type.value,
                "template": k.template_id,
                "intent": k.primary_intent.value,
                "status": k.candidate_status.value,
                "priority": round(k.priority_score, 2),
                "first_discovered_at": k.first_discovered_at.isoformat(),
                "evidence_class": "KEYWORD_HYPOTHESIS",
            }
            for k, q in rows
        ]

    def corroboration(self, entity_id: int | None) -> dict[str, Any]:
        """Independent non-GitHub publishers mentioning the entity (reposts count once)."""
        if entity_id is None:
            return {"independent_publishers": 0, "providers": [], "basis": "no entity linked"}
        rows = self._s.execute(
            sa.select(
                DiscoveryItemRow.provider_name,
                sa.func.coalesce(DiscoveryItemRow.publisher, DiscoveryItemRow.canonical_url).label(
                    "origin"
                ),
            )
            .join(EntityMentionRow, EntityMentionRow.item_id == DiscoveryItemRow.id)
            .where(
                EntityMentionRow.entity_id == entity_id,
                EntityMentionRow.status != EntityStatus.REJECTED,
                DiscoveryItemRow.provider_name != "github",
                DiscoveryItemRow.is_synthetic.is_(False),
            )
            .distinct()
        ).all()
        origins = {str(o).lower() for _p, o in rows if o}
        return {
            "independent_publishers": len(origins),
            "providers": sorted({p for p, _o in rows}),
            "basis": "distinct publishers outside GitHub mentioning the entity; reposts of "
            "one announcement share a publisher or URL and count once",
        }

    def entity(self, entity_id: int | None) -> DiscoveredEntityRow | None:
        return self._s.get(DiscoveredEntityRow, entity_id) if entity_id else None

    def stats(self) -> dict[str, Any]:
        def one(q: Any) -> Any:
            return self._s.execute(q).scalar_one()

        return {
            "repositories": int(one(sa.select(sa.func.count()).select_from(GitHubRepositoryRow))),
            "observations": int(one(sa.select(sa.func.count()).select_from(GitHubObservationRow))),
            "releases": int(one(sa.select(sa.func.count()).select_from(GitHubReleaseRow))),
            "last_observed_at": one(sa.select(sa.func.max(GitHubObservationRow.observed_at))),
            "last_release_seen_at": one(sa.select(sa.func.max(GitHubReleaseRow.last_seen_at))),
        }

    # ------------------------------------------------------------------ retention
    _EXPIRING: Sequence[tuple[str, Any]] = (
        ("github_repository_observations", GitHubObservationRow),
        ("github_releases", GitHubReleaseRow),
        ("github_repositories", GitHubRepositoryRow),
    )

    def count_expired(self, now: datetime) -> dict[str, int]:
        return {
            name: int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.expires_at <= now)
                ).scalar_one()
            )
            for name, model in self._EXPIRING
        }

    def purge_expired(self, now: datetime) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, model in self._EXPIRING:
            res = self._s.execute(sa.delete(model).where(model.expires_at <= now))
            out[name] = int(getattr(res, "rowcount", 0) or 0)
        return out
