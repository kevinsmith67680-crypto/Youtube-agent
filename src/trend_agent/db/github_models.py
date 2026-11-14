"""GitHub tables: repositories, repeated observations, releases.

Relationships::

    github_repositories 1--* github_repository_observations
    github_repositories 1--* github_releases
    github_repositories *--0..1 discovery_items      (the repository's discovery item)
    github_repositories *--0..1 discovered_entities  (the entity it was resolved to)

Invariants: the repository key is GitHub's stable numeric ID (renames update
``full_name``); one observation per (repository, collection time) so re-ingestion is
idempotent; one release row per GitHub release ID; counts are non-negative. Only
metadata is stored: no README, no code, no issue text, no user data beyond owner logins.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.discovery.models import Category

RELEASE_LEVELS = ("major", "minor", "patch", "prerelease", "unknown")


class GitHubRepositoryRow(Base):
    __tablename__ = "github_repositories"
    __table_args__ = (
        sa.CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
        sa.CheckConstraint("discovery_score >= 0 AND discovery_score <= 100", name="score_range"),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        sa.CheckConstraint(
            "latest_stargazers_count >= 0 AND latest_forks_count >= 0 AND size_kb >= 0",
            name="non_negative_counts",
        ),
        sa.Index("ix_github_repositories_first_seen", "category", "first_seen_at"),
        sa.Index("ix_github_repositories_full_name", "full_name"),
        sa.Index("ix_github_repositories_expires_at", "expires_at"),
    )

    repository_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(sa.String(200))
    name: Mapped[str] = mapped_column(sa.String(100))
    owner_login: Mapped[str] = mapped_column(sa.String(100))
    owner_type: Mapped[str | None] = mapped_column(sa.String(20))
    html_url: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)  # sanitised, <= 300 chars
    language: Mapped[str | None] = mapped_column(sa.String(64))
    topics: Mapped[list[str]] = mapped_column(ARRAY(sa.String(64)), server_default="{}")
    license_spdx: Mapped[str | None] = mapped_column(sa.String(64))
    created_at_github: Mapped[datetime]
    latest_pushed_at: Mapped[datetime | None]
    latest_updated_at: Mapped[datetime | None]
    latest_stargazers_count: Mapped[int] = mapped_column(sa.Integer)
    latest_forks_count: Mapped[int] = mapped_column(sa.Integer)
    size_kb: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    archived: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    is_fork: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    category: Mapped[Category] = mapped_column(str_enum(Category, "github_repository_category"))
    strategies: Mapped[list[str]] = mapped_column(ARRAY(sa.String(32)), server_default="{}")
    relevance: Mapped[float] = mapped_column(sa.Double, server_default="0")
    discovery_score: Mapped[float] = mapped_column(sa.Double, server_default="0")
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(sa.String(48)), server_default="{}")
    entity_type: Mapped[str | None] = mapped_column(sa.String(40))
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    first_source_key: Mapped[str] = mapped_column(sa.String(200))
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=utc_now_sql(), onupdate=sa.func.now()
    )


class GitHubObservationRow(Base):
    __tablename__ = "github_repository_observations"
    __table_args__ = (
        sa.UniqueConstraint("repository_id", "observed_at", name="observation_once"),
        sa.CheckConstraint(
            "stargazers_count >= 0 AND forks_count >= 0 AND open_issues_count >= 0",
            name="non_negative_counts",
        ),
        sa.CheckConstraint(
            "collection_status IN ('collected','partial')", name="known_collection_status"
        ),
        sa.Index("ix_github_observations_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        sa.ForeignKey("github_repositories.repository_id", ondelete="CASCADE")
    )
    observed_at: Mapped[datetime]
    stargazers_count: Mapped[int] = mapped_column(sa.Integer)
    forks_count: Mapped[int] = mapped_column(sa.Integer)
    open_issues_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    pushed_at_github: Mapped[datetime | None]
    updated_at_github: Mapped[datetime | None]
    size_kb: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    archived: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    strategy: Mapped[str] = mapped_column(sa.String(32))
    source_key: Mapped[str] = mapped_column(sa.String(200))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    collection_status: Mapped[str] = mapped_column(sa.String(16), server_default="collected")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())


class GitHubReleaseRow(Base):
    __tablename__ = "github_releases"
    __table_args__ = (
        sa.CheckConstraint(
            "release_level IN ('major','minor','patch','prerelease','unknown')",
            name="known_release_level",
        ),
        sa.Index("ix_github_releases_published", "published_at_github"),
        sa.Index("ix_github_releases_repository", "repository_full_name", "published_at_github"),
        sa.Index("ix_github_releases_expires_at", "expires_at"),
    )

    release_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    repository_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("github_repositories.repository_id", ondelete="CASCADE")
    )
    repository_full_name: Mapped[str] = mapped_column(sa.String(200))
    tag_name: Mapped[str] = mapped_column(sa.String(255))
    name: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[str | None] = mapped_column(sa.String(64))
    release_level: Mapped[str] = mapped_column(sa.String(16))
    prerelease: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    html_url: Mapped[str] = mapped_column(sa.Text)
    published_at_github: Mapped[datetime | None]
    created_at_github: Mapped[datetime | None]
    is_breaking: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    breaking_lines: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    capability_lines: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    api_lines: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    analysis_flags: Mapped[list[str]] = mapped_column(ARRAY(sa.String(48)), server_default="{}")
    category: Mapped[Category] = mapped_column(str_enum(Category, "github_release_category"))
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    source_key: Mapped[str] = mapped_column(sa.String(200))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
