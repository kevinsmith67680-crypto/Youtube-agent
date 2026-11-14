"""Construct and register discovery providers from settings + discovery configuration."""

from __future__ import annotations

from datetime import timedelta

import httpx

from trend_agent.config import Settings
from trend_agent.discovery.config import LoadedDiscoveryConfig
from trend_agent.discovery.providers.base import HttpFetcher, request_quota
from trend_agent.discovery.providers.community import HackerNewsProvider, RedditProvider
from trend_agent.discovery.providers.feeds import FeedDiscoveryProvider
from trend_agent.discovery.providers.github import GitHubDiscoveryProvider
from trend_agent.discovery.providers.hackernews_firebase import HackerNewsDiscoveryProvider
from trend_agent.discovery.providers.local import CuratedSeedProvider, TrendsRelatedQueriesProvider
from trend_agent.discovery.providers.twitch import TwitchDiscoveryProvider
from trend_agent.discovery.providers.youtube import YouTubeDiscoveryProvider
from trend_agent.github.client import build_client
from trend_agent.github.config import load_github_settings
from trend_agent.hackernews.client import build_client as build_hn_client
from trend_agent.hackernews.config import load_hackernews_settings
from trend_agent.providers.base import Provider
from trend_agent.providers.errors import ProviderNotFoundError
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import QuotaStore
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.twitch.client import build_client as build_twitch_client
from trend_agent.twitch.config import load_twitch_settings
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
        max_attempts=settings.retry_attempts,
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
    p = cfg.provider("github")
    gh = load_github_settings(p.params)
    add(
        GitHubDiscoveryProvider(
            build_client(
                http_client=http_client,
                token=settings.github_token if settings.github_api_enabled else None,
                quota_store=quota_store,
                clock=clock,
                retry=retry,
                base_url=settings.github_api_base_url,
                api_version=settings.github_api_version,
                user_agent=ua,
                core_daily=gh.api.core_daily_budget,
                search_daily=gh.api.search_daily_budget,
                max_pages=gh.api.max_pages,
                min_remaining={
                    "core": gh.api.min_remaining_core,
                    "search": gh.api.min_remaining_search,
                },
                dedup_ttl=timedelta(seconds=gh.api.dedup_ttl_seconds),
            ),
            settings=gh,
            enabled=p.enabled,
            api_enabled=settings.github_api_enabled,
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
    p = cfg.provider("hackernews_firebase")
    hn = load_hackernews_settings(p.params)
    add(
        HackerNewsDiscoveryProvider(
            build_hn_client(
                http_client=http_client,
                quota_store=quota_store,
                clock=clock,
                retry=retry,
                base_url=hn.base_url,
                user_agent=ua,
                daily_budget=hn.client.daily_request_budget,
                max_concurrency=hn.client.max_concurrency,
                item_cache_seconds=hn.client.item_cache_seconds,
                list_cache_seconds=hn.client.list_cache_seconds,
            ),
            settings=hn,
            enabled=p.enabled,
            api_enabled=settings.hackernews_api_enabled,
            retention_days=p.retention_days,
            clock=clock,
        )
    )
    p = cfg.provider("twitch")
    tw = load_twitch_settings(p.params)
    add(
        TwitchDiscoveryProvider(
            build_twitch_client(
                http_client=http_client,
                quota_store=quota_store,
                clock=clock,
                retry=retry,
                client_id=settings.twitch_client_id,
                client_secret=settings.twitch_client_secret,
                helix_base_url=tw.helix_base_url,
                auth_base_url=tw.auth_base_url,
                user_agent=ua,
                daily_budget=tw.client.daily_request_budget,
                max_concurrency=tw.client.max_concurrency,
                page_size=tw.client.page_size,
                rate_limit_reserve=tw.client.rate_limit_reserve,
                max_rate_limit_wait=tw.client.max_rate_limit_wait_seconds,
                token_refresh_margin_seconds=tw.client.token_refresh_margin_seconds,
                token_validate_interval_seconds=tw.client.token_validate_interval_seconds,
            ),
            settings=tw,
            enabled=p.enabled,
            api_enabled=settings.twitch_api_enabled,
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
