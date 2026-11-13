"""Construct and register discovery providers from settings + discovery configuration."""

from __future__ import annotations

import httpx

from trend_agent.config import Settings
from trend_agent.discovery.config import LoadedDiscoveryConfig
from trend_agent.discovery.providers.base import HttpFetcher, request_quota
from trend_agent.discovery.providers.community import (
    GitHubReleasesProvider,
    HackerNewsProvider,
    RedditProvider,
)
from trend_agent.discovery.providers.feeds import FeedDiscoveryProvider
from trend_agent.discovery.providers.local import CuratedSeedProvider, TrendsRelatedQueriesProvider
from trend_agent.discovery.providers.youtube import YouTubeDiscoveryProvider
from trend_agent.providers.base import Provider
from trend_agent.providers.errors import ProviderNotFoundError
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import QuotaStore
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.utils.time import Clock


def register_discovery_providers(
    registry: ProviderRegistry,
    *,
    settings: Settings,
    loaded: LoadedDiscoveryConfig,
    quota_store: QuotaStore,
    clock: Clock,
    http_client: httpx.AsyncClient,
) -> None:
    """Idempotently add discovery providers to ``registry``."""
    cfg = loaded.config
    retry = RetryPolicy(
        max_attempts=settings.http_max_attempts,
        base_delay=settings.http_backoff_base_seconds,
        max_delay=settings.http_backoff_max_seconds,
    )
    ua = settings.http_user_agent

    def fetcher(name: str) -> HttpFetcher:
        return HttpFetcher(
            provider_name=name,
            client=http_client,
            quota=request_quota(name, cfg.provider(name).daily_request_limit, quota_store, clock),
            retry=retry,
            clock=clock,
        )

    def add(provider: Provider) -> None:
        if provider.descriptor.name not in registry.names():
            registry.register(provider)

    try:
        youtube = registry.get("youtube_data_api")
    except ProviderNotFoundError:
        youtube = None
    if isinstance(youtube, YouTubeDataApiProvider):
        p = cfg.provider("youtube_discovery")
        add(
            YouTubeDiscoveryProvider(
                youtube,
                enabled=p.enabled,
                retention_days=p.retention_days,
                results_per_query=int(p.params.get("search_results_per_query", 25)),
                default_region=cfg.regions[0] if cfg.regions else "US",
                default_language=cfg.languages[0] if cfg.languages else None,
                discovery_quota=request_quota(
                    "youtube_discovery", p.daily_request_limit, quota_store, clock
                ),
            )
        )
    p = cfg.provider("feeds")
    add(
        FeedDiscoveryProvider(
            fetcher=fetcher("feeds"),
            user_agent=ua,
            enabled=p.enabled,
            retention_days=p.retention_days,
            clock=clock,
        )
    )
    p = cfg.provider("github_releases")
    add(
        GitHubReleasesProvider(
            fetcher=fetcher("github_releases"),
            token=settings.github_token,
            user_agent=ua,
            enabled=p.enabled,
            retention_days=p.retention_days,
            clock=clock,
        )
    )
    p = cfg.provider("hackernews")
    add(
        HackerNewsProvider(
            fetcher=fetcher("hackernews"),
            user_agent=ua,
            enabled=p.enabled,
            retention_days=p.retention_days,
            clock=clock,
        )
    )
    p = cfg.provider("reddit")
    add(
        RedditProvider(
            fetcher=fetcher("reddit"),
            client=http_client,
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=ua,
            enabled=p.enabled,
            retention_days=p.retention_days,
            clock=clock,
        )
    )
    try:
        trends = registry.get("google_trends")
    except ProviderNotFoundError:
        trends = None
    if isinstance(trends, GoogleTrendsProvider):
        add(
            TrendsRelatedQueriesProvider(
                trends, enabled=cfg.provider("google_trends_discovery").enabled, clock=clock
            )
        )
    add(CuratedSeedProvider(enabled=cfg.provider("curated_seeds").enabled, clock=clock))
