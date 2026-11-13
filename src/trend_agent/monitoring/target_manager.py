"""Monitoring target manager: which discovered keywords are monitored, how, and how closely.

Activation is selective. A keyword is considered when its discovery status is PRIORITIZED
(or already MONITORING) and its discovery priority clears ``activation.min_priority``. It
becomes ACTIVE only if

1. a provider that can measure the required category is actually available (synthetic
   keywords only ever use synthetic providers; real keywords never do),
2. its discovery evidence clears the quality floor (independent origins),
3. there is monitoring capacity left in its category (45/25/30 by default, unused share
   redistributed, ``exploration_share`` reserved for new subjects).

Otherwise it stays PENDING (capacity) or PROVIDER_UNAVAILABLE (no source) - it is never
presented as being under search-demand monitoring. Proxy targets (event signals) are a
separate, explicitly labelled track.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import DiscoveredKeywordRow
from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.discovery import DiscoveryRepository
from trend_agent.db.repositories.monitoring import KeywordCandidateView, MonitoringRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.lifecycle import MONITORING_ACTOR, transition
from trend_agent.discovery.models import TARGET_CATEGORIES, CandidateStatus, Category
from trend_agent.monitoring.collectors import CollectorRegistry, TargetSnapshot
from trend_agent.monitoring.config import MonitoringConfig
from trend_agent.monitoring.lifecycle import (
    MANAGER_ACTOR,
    MANUAL_ACTOR,
    InvalidMonitoringTransitionError,
    monitoring_transition,
)
from trend_agent.schemas.monitoring import (
    COLLECTABLE_STATUSES,
    TIER_ORDER,
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
)
from trend_agent.utils.time import Clock

M = MonitoringStatus
CAPABLE_WHEN_DOWN = frozenset({"temporarily_unavailable", "rate_limited", "quota_exhausted"})
ACTIVE_LIKE = tuple(COLLECTABLE_STATUSES)


@dataclass
class SyncReport:
    candidates_seen: int = 0
    targets_created: int = 0
    activated: int = 0
    pending_capacity: int = 0
    provider_unavailable: int = 0
    demoted_dormant: int = 0
    disabled: int = 0
    recovered: int = 0
    provider_switches: int = 0
    tier_changes: int = 0
    keyword_status_changes: int = 0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderChoice:
    measurement: MeasurementCategory
    preferred: str | None
    fallbacks: tuple[str, ...]
    in_use: str | None
    reasons: tuple[str, ...]


def compute_tier(
    config: MonitoringConfig,
    *,
    now: datetime,
    priority: float,
    novelty_kind: str | None,
    entity_type: str | None,
    entity_first_seen: datetime | None,
    time_sensitive_until: datetime | None,
    status: MonitoringStatus,
    pinned: str | None = None,
) -> tuple[MonitoringTier, str]:
    """Operational tier from discovery metadata only (no demand estimates)."""
    if pinned:
        return MonitoringTier(pinned), "manual override"
    if status is M.DORMANT:
        return MonitoringTier.DORMANT, "target dormant"
    tiers = config.tiers
    sensitive_types = {t.value for t in config.time_sensitive_entity_types}
    age = (now - entity_first_seen) if entity_first_seen else None
    if time_sensitive_until is not None:
        sensitive = entity_type in sensitive_types and now <= time_sensitive_until
    else:
        window = tiers.critical.time_window
        sensitive = (
            entity_type in sensitive_types
            and age is not None
            and window is not None
            and age <= window
        )
    new_subject = bool(novelty_kind and novelty_kind.startswith("new_"))
    if sensitive and new_subject and priority >= tiers.critical.min_priority:
        return MonitoringTier.CRITICAL, "new, time-sensitive subject with high discovery priority"
    if novelty_kind == "evergreen":
        return MonitoringTier.LOW, "established evergreen subject (baseline cadence)"
    high_window = tiers.high.time_window
    recent = age is not None and high_window is not None and age <= high_window
    if new_subject and recent and priority >= tiers.high.min_priority:
        return MonitoringTier.HIGH, "new subject with strong discovery evidence"
    if priority >= tiers.critical.min_priority:
        return MonitoringTier.HIGH, "high discovery priority"
    if priority >= tiers.normal.min_priority:
        return MonitoringTier.NORMAL, "established subject; baseline maintenance"
    return MonitoringTier.LOW, "lower discovery priority"


def apply_tier_hysteresis(
    current: MonitoringTier | None,
    proposed: MonitoringTier,
    changed_at: datetime | None,
    now: datetime,
    hold: timedelta,
) -> MonitoringTier:
    """Raise immediately; lower only after the current tier has held for ``hold``."""
    if current is None or current is proposed:
        return proposed
    if TIER_ORDER.index(proposed) < TIER_ORDER.index(current):
        return proposed
    if proposed is MonitoringTier.DORMANT:
        return proposed  # follows the status change
    if changed_at is not None and now - changed_at < hold:
        return current
    return proposed


def set_status(
    repo: MonitoringRepository,
    target: MonitoringTargetRow,
    new: MonitoringStatus,
    *,
    reason: str,
    actor: str,
    now: datetime,
    details: dict[str, object] | None = None,
) -> bool:
    """Validated, logged status change. Returns False if already in that status."""
    if target.status is new:
        target.status_reason = reason
        return False
    t = monitoring_transition(target.status, new, reason=reason, actor=actor, at=now)
    repo.add_event(
        target.id,
        kind="status",
        from_value=target.status.value,
        to_value=new.value,
        reason=reason,
        actor=actor,
        at=now,
        details=dict(details or {}),
    )
    target.status = t.to_status
    target.status_reason = reason
    target.status_changed_at = now
    if new is M.ACTIVE and target.activated_at is None:
        target.activated_at = now
    return True


def snapshot(target: MonitoringTargetRow, canonical: str, original: str) -> TargetSnapshot:
    return TargetSnapshot(
        target_id=target.id,
        keyword_id=target.keyword_id,
        query_id=target.query_id,
        canonical_query=canonical,
        original_query=original,
        region=target.region,
        language=target.language,
        measurement_category=target.measurement_category,
        is_proxy=target.is_proxy,
        is_synthetic=target.is_synthetic,
    )


class TargetManager:
    def __init__(
        self,
        session_factory: SessionFactory,
        config: MonitoringConfig,
        collectors: CollectorRegistry,
        *,
        clock: Clock,
        include_synthetic: bool | None = None,
    ) -> None:
        self._factory = session_factory
        self._config = config
        self._collectors = collectors
        self._clock = clock
        self._include_synthetic = (
            config.activation.include_synthetic if include_synthetic is None else include_synthetic
        )

    # ------------------------------------------------------------------ provider choice
    def choose_provider(self, *, synthetic: bool, proxy: bool = False) -> ProviderChoice:
        reasons: list[str] = []
        if proxy:
            name = self._config.proxy_monitoring.provider
            avail = self._collectors.availability(name)
            measurement = MeasurementCategory.OFFICIAL_EVENT_SIGNAL
            if avail.available:
                return ProviderChoice(measurement, name, (), name, ())
            return ProviderChoice(measurement, name, (), None, (f"{name}: {avail.detail}",))
        first: ProviderChoice | None = None
        for measurement in self._config.measurement_preferences:
            names = []
            for name in self._config.providers_for(measurement):
                profile = self._collectors.profile(name)
                if profile is None or measurement not in profile.measurement_categories:
                    continue
                if profile.synthetic != synthetic:
                    continue  # never mix synthetic and real sources
                names.append(name)
            available: list[str] = []
            capable: list[str] = []
            for name in names:
                avail = self._collectors.availability(name)
                if avail.available:
                    available.append(name)
                    capable.append(name)
                else:
                    reasons.append(f"{name} ({measurement.value}): {avail.status} - {avail.detail}")
                    if avail.status in CAPABLE_WHEN_DOWN:
                        capable.append(name)  # configured; only temporarily unavailable
            if capable:
                # A transient outage must not switch a keyword to another measurement.
                return ProviderChoice(
                    measurement,
                    capable[0],
                    tuple(capable[1:]),
                    available[0] if available else None,
                    tuple(reasons),
                )
            if first is None:
                first = ProviderChoice(
                    measurement, names[0] if names else None, tuple(names[1:]), None, ()
                )
        if not reasons:
            reasons.append("no provider configured for the preferred measurements")
        assert first is not None
        return ProviderChoice(
            first.measurement, first.preferred, first.fallbacks, None, tuple(reasons)
        )

    # ------------------------------------------------------------------ sync
    def sync(self, *, keyword_ids: Sequence[int] | None = None) -> SyncReport:
        now = self._clock()
        report = SyncReport()
        cfg = self._config
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            search_choice = {s: self.choose_provider(synthetic=s) for s in (False, True)}
            proxy_choice = self.choose_provider(synthetic=False, proxy=True)
            after: int | None = None
            while True:
                page = repo.keyword_candidates(
                    statuses=cfg.activation.keyword_statuses,
                    min_priority=cfg.activation.min_priority,
                    include_synthetic=self._include_synthetic,
                    after_id=after,
                    limit=cfg.capacity.planning_page_size,
                    keyword_ids=keyword_ids,
                )
                if not page:
                    break
                after = page[-1].keyword_id
                for kw in page:
                    report.candidates_seen += 1
                    if kw.category not in TARGET_CATEGORIES:
                        continue
                    choice = search_choice[kw.is_synthetic]
                    for region in cfg.regions:
                        for language in cfg.languages:
                            if self._ensure_target(repo, kw, region, language, choice, False, now):
                                report.targets_created += 1
                    if (
                        cfg.proxy_monitoring.enabled
                        and not kw.is_synthetic
                        and self._ensure_target(repo, kw, "WORLD", None, proxy_choice, True, now)
                    ):
                        report.targets_created += 1
            self._evaluate(session, repo, report, now, search_choice, proxy_choice)
            self._sync_keyword_status(session, repo, report, now)
        return report

    def _ensure_target(
        self,
        repo: MonitoringRepository,
        kw: KeywordCandidateView,
        region: str,
        language: str | None,
        choice: ProviderChoice,
        proxy: bool,
        now: datetime,
    ) -> bool:
        existing = repo.find_target(kw.query_id, region, language, choice.measurement)
        if existing is not None:
            return False
        tier, tier_reason = compute_tier(
            self._config,
            now=now,
            priority=kw.priority,
            novelty_kind=kw.novelty_kind,
            entity_type=kw.entity_type,
            entity_first_seen=kw.entity_first_seen_at,
            time_sensitive_until=None,
            status=M.PENDING,
        )
        status = M.PENDING if choice.in_use else M.PROVIDER_UNAVAILABLE
        reason = (
            "selected for monitoring; awaiting activation"
            if choice.in_use
            else "no available provider: " + "; ".join(choice.reasons)
        )
        row, created = repo.create_target(
            {
                "keyword_id": kw.keyword_id,
                "query_id": kw.query_id,
                "category": kw.category,
                "topic_cluster_id": kw.topic_cluster_id,
                "intent_cluster_id": kw.intent_cluster_id,
                "region": region,
                "language": language,
                "measurement_category": choice.measurement,
                "is_proxy": proxy,
                "preferred_provider": choice.preferred,
                "fallback_providers": list(choice.fallbacks),
                "provider_in_use": choice.in_use,
                "tier": tier,
                "tier_reason": tier_reason,
                "tier_changed_at": now,
                "status": status,
                "status_reason": reason,
                "status_changed_at": now,
                "priority_score": kw.priority,
                "novelty_kind": kw.novelty_kind,
                "entity_type": kw.entity_type,
                "entity_first_seen_at": kw.entity_first_seen_at,
                "config_version": self._config.config_version,
                "is_synthetic": kw.is_synthetic,
            }
        )
        if created:
            repo.add_event(
                row.id,
                kind="status",
                from_value=None,
                to_value=status.value,
                reason=reason,
                actor=MANAGER_ACTOR,
                at=now,
            )
        return created

    def _evaluate(
        self,
        session: Session,
        repo: MonitoringRepository,
        report: SyncReport,
        now: datetime,
        search_choice: dict[bool, ProviderChoice],
        proxy_choice: ProviderChoice,
    ) -> None:
        cfg = self._config
        act = cfg.activation
        activation_queue: dict[tuple[Category, bool], list[MonitoringTargetRow]] = defaultdict(list)
        after: int | None = None
        while True:
            page = repo.targets(
                statuses=[s for s in M if s is not M.DISABLED],
                include_synthetic=self._include_synthetic,
                after_id=after,
                limit=cfg.capacity.planning_page_size,
            )
            if not page:
                break
            after = page[-1].id
            keywords = repo.keywords_by_ids([t.keyword_id for t in page if t.keyword_id])
            for target in page:
                kw = keywords.get(target.keyword_id) if target.keyword_id else None
                override = target.manual_override or {}
                if kw is not None:
                    target.priority_score = kw.priority_score
                    target.category = kw.primary_category
                    target.topic_cluster_id = kw.topic_cluster_id
                    target.intent_cluster_id = kw.intent_cluster_id
                    target.novelty_kind = kw.novelty_kind.value
                target.config_version = cfg.config_version
                if override.get("excluded"):
                    if set_status(
                        repo,
                        target,
                        M.DISABLED,
                        reason="manually excluded",
                        actor=MANAGER_ACTOR,
                        now=now,
                    ):
                        report.disabled += 1
                    continue
                if kw is not None and kw.candidate_status is CandidateStatus.REJECTED:
                    if set_status(
                        repo,
                        target,
                        M.DISABLED,
                        reason="keyword rejected by discovery",
                        actor=MANAGER_ACTOR,
                        now=now,
                    ):
                        report.disabled += 1
                    continue
                if target.status is M.PAUSED or override.get("backfill_only"):
                    continue  # manual decisions; backfill-only targets are not monitored
                choice = proxy_choice if target.is_proxy else search_choice[target.is_synthetic]
                usable = self._usable_provider(target)
                if usable is None:
                    if set_status(
                        repo,
                        target,
                        M.PROVIDER_UNAVAILABLE,
                        reason="no available provider: " + "; ".join(choice.reasons),
                        actor=MANAGER_ACTOR,
                        now=now,
                    ):
                        report.provider_unavailable += 1
                    self._retier(repo, target, now, report)
                    continue
                if usable != target.provider_in_use:
                    repo.add_event(
                        target.id,
                        kind="provider",
                        from_value=target.provider_in_use,
                        to_value=usable,
                        reason="provider switch (availability)",
                        actor=MANAGER_ACTOR,
                        at=now,
                    )
                    target.provider_in_use = usable
                    report.provider_switches += 1
                if target.status in ACTIVE_LIKE:
                    if self._should_demote(target, kw, now):
                        set_status(
                            repo,
                            target,
                            M.DORMANT,
                            reason=self._demotion_reason(target, kw, now),
                            actor=MANAGER_ACTOR,
                            now=now,
                        )
                        report.demoted_dormant += 1
                    self._retier(repo, target, now, report)
                    continue
                if target.status is M.DORMANT:
                    cooldown_over = now - target.status_changed_at >= act.reactivation_cooldown
                    if not (cooldown_over and kw is not None and self._eligible(kw, now)):
                        self._retier(repo, target, now, report)
                        continue
                if target.status in (M.PENDING, M.PROVIDER_UNAVAILABLE, M.DORMANT):
                    if kw is not None and not self._eligible(kw, now):
                        self._retier(repo, target, now, report)
                        continue
                    activation_queue[(target.category, target.is_proxy)].append(target)
                    if target.status is M.PROVIDER_UNAVAILABLE:
                        report.recovered += 1
            session.flush()
        self._activate(repo, activation_queue, report, now)

    def _usable_provider(self, target: MonitoringTargetRow) -> str | None:
        for name in [target.preferred_provider, *target.fallback_providers]:
            if name and self._collectors.availability(name).available:
                return name
        return None

    def _eligible(self, kw: DiscoveredKeywordRow, now: datetime) -> bool:
        act = self._config.activation
        origins = int((kw.score_components or {}).get("independent_origins", 0))
        return (
            kw.candidate_status in act.keyword_statuses
            and kw.priority_score >= act.min_priority
            and origins >= act.min_independent_origins
        )

    def _should_demote(
        self, target: MonitoringTargetRow, kw: DiscoveredKeywordRow | None, now: datetime
    ) -> bool:
        act = self._config.activation
        if target.manual_override.get("pinned_active"):
            return False
        active_since = target.activated_at or target.status_changed_at
        if now - active_since < act.min_active:
            return False  # minimum active duration (anti-thrashing)
        if target.time_sensitive_until is not None and now <= target.time_sensitive_until:
            return False
        if kw is None:
            return False  # manual target: only manual actions demote it
        if kw.priority_score < act.deactivate_below:
            return True
        return now - kw.last_discovered_at > act.dormant_after_no_evidence

    def _demotion_reason(
        self, target: MonitoringTargetRow, kw: DiscoveredKeywordRow | None, now: datetime
    ) -> str:
        act = self._config.activation
        if kw is not None and kw.priority_score < act.deactivate_below:
            return f"discovery priority {kw.priority_score} below {act.deactivate_below}"
        return f"no new discovery evidence for {act.dormant_after_no_evidence.days} days"

    def _retier(
        self,
        repo: MonitoringRepository,
        target: MonitoringTargetRow,
        now: datetime,
        report: SyncReport,
    ) -> None:
        proposed, reason = compute_tier(
            self._config,
            now=now,
            priority=target.priority_score,
            novelty_kind=target.novelty_kind,
            entity_type=target.entity_type,
            entity_first_seen=target.entity_first_seen_at,
            time_sensitive_until=target.time_sensitive_until,
            status=target.status,
            pinned=(target.manual_override or {}).get("pinned_tier"),
        )
        tier = apply_tier_hysteresis(
            target.tier, proposed, target.tier_changed_at, now, self._config.tiers.downgrade_hold
        )
        if tier is not target.tier:
            repo.add_event(
                target.id,
                kind="tier",
                from_value=target.tier.value,
                to_value=tier.value,
                reason=reason,
                actor=MANAGER_ACTOR,
                at=now,
            )
            target.tier = tier
            target.tier_reason = reason
            target.tier_changed_at = now
            report.tier_changes += 1
        elif tier is proposed:
            target.tier_reason = reason

    def _is_exploratory(self, target: MonitoringTargetRow, now: datetime) -> bool:
        window = self._config.exploration_window
        new_subject = bool(target.novelty_kind and target.novelty_kind.startswith("new_"))
        recent = target.entity_first_seen_at is not None and (
            now - target.entity_first_seen_at <= window
        )
        return new_subject and recent

    def _activate(
        self,
        repo: MonitoringRepository,
        queue: dict[tuple[Category, bool], list[MonitoringTargetRow]],
        report: SyncReport,
        now: datetime,
    ) -> None:
        cfg = self._config
        for proxy in (False, True):
            total = (
                cfg.capacity.max_active_proxy_targets if proxy else cfg.capacity.max_active_targets
            )
            active = repo.count_targets(statuses=ACTIVE_LIKE, proxy=proxy)
            shares = {c: cfg.category_budgets.share(c) for c in TARGET_CATEGORIES}
            capacity = {c: math.floor(total * shares[c]) for c in TARGET_CATEGORIES}
            free = {c: max(0, capacity[c] - active.get(c, 0)) for c in TARGET_CATEGORIES}
            # Unused share flows to categories that have more candidates than room.
            demand = {c: len(queue.get((c, proxy), [])) for c in TARGET_CATEGORIES}
            spare = sum(max(0, free[c] - demand[c]) for c in TARGET_CATEGORIES)
            spare += max(0, total - sum(capacity.values()))
            for c in sorted(TARGET_CATEGORIES, key=lambda c: -shares[c]):
                extra = min(spare, max(0, demand[c] - free[c]))
                free[c] += extra
                spare -= extra
            for category in TARGET_CATEGORIES:
                candidates = queue.get((category, proxy), [])
                if not candidates:
                    continue
                stats = report.by_category.setdefault(category.value, {})
                explore_room = math.ceil(free[category] * cfg.exploration_share)
                exploratory = sorted(
                    (t for t in candidates if self._is_exploratory(t, now)),
                    key=lambda t: (-t.priority_score, t.id),
                )
                ordered = exploratory[:explore_room]
                chosen_ids = {t.id for t in ordered}
                ordered += sorted(
                    (t for t in candidates if t.id not in chosen_ids),
                    key=lambda t: (-t.priority_score, t.id),
                )
                for i, target in enumerate(ordered):
                    if i < free[category]:
                        set_status(
                            repo,
                            target,
                            M.ACTIVE,
                            reason=(
                                "activated: provider available, capacity in category "
                                f"{category.value}"
                                + (
                                    " (exploration reserve)"
                                    if self._is_exploratory(target, now)
                                    else ""
                                )
                            ),
                            actor=MANAGER_ACTOR,
                            now=now,
                            details={"provider": target.provider_in_use, "proxy": proxy},
                        )
                        target.next_collection_at = now
                        self._retier(repo, target, now, report)
                        report.activated += 1
                        stats["activated"] = stats.get("activated", 0) + 1
                    else:
                        if target.status is not M.PENDING:
                            set_status(
                                repo,
                                target,
                                M.PENDING,
                                reason="waiting for monitoring capacity",
                                actor=MANAGER_ACTOR,
                                now=now,
                            )
                        else:
                            target.status_reason = "waiting for monitoring capacity"
                        report.pending_capacity += 1
                        stats["pending_capacity"] = stats.get("pending_capacity", 0) + 1

    def _sync_keyword_status(
        self, session: Session, repo: MonitoringRepository, report: SyncReport, now: datetime
    ) -> None:
        """Mirror monitoring into the discovery lifecycle (actor: monitoring)."""
        discovery = DiscoveryRepository(session)
        targets = repo.targets(
            proxy=False, include_synthetic=self._include_synthetic, limit=100_000
        )
        by_keyword: dict[int, list[MonitoringTargetRow]] = defaultdict(list)
        for t in targets:
            if t.keyword_id is not None:
                by_keyword[t.keyword_id].append(t)
        keywords = repo.keywords_by_ids(list(by_keyword))
        for keyword_id, rows in by_keyword.items():
            kw = keywords.get(keyword_id)
            if kw is None:
                continue
            monitored = any(t.status in ACTIVE_LIKE for t in rows)
            target_status: CandidateStatus | None = None
            if monitored and kw.candidate_status is CandidateStatus.PRIORITIZED:
                target_status = CandidateStatus.MONITORING
                reason = "search-interest monitoring target active"
            elif not monitored and kw.candidate_status is CandidateStatus.MONITORING:
                all_dormant = all(t.status is M.DORMANT for t in rows)
                target_status = (
                    CandidateStatus.DORMANT if all_dormant else CandidateStatus.PRIORITIZED
                )
                reason = "monitoring target " + ("dormant" if all_dormant else "no longer active")
            if target_status is None:
                continue
            t_ = transition(
                kw.candidate_status, target_status, reason=reason, actor=MONITORING_ACTOR, at=now
            )
            kw.candidate_status = target_status
            kw.status_reason = reason
            discovery.add_history(kw.id, t_, None)
            report.keyword_status_changes += 1

    # ------------------------------------------------------------------ manual operations
    def _resolve(self, repo: MonitoringRepository, query: str) -> list[MonitoringTargetRow]:
        from trend_agent.utils.text import canonicalize_query

        return repo.targets(query_text=canonicalize_query(query), limit=100)

    def activate(self, query: str, *, proxy: bool = False) -> tuple[bool, str]:
        """Manually activate monitoring for an existing discovered keyword."""
        from trend_agent.utils.text import canonicalize_query

        now = self._clock()
        canonical = canonicalize_query(query)
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            kw = repo.keyword_for_query(canonical)
            if kw is None:
                return False, f"no discovered keyword {canonical!r}; run discovery first"
            choice = self.choose_provider(synthetic=kw.is_synthetic, proxy=proxy)
            view = repo.keyword_candidates(
                statuses=list(CandidateStatus),
                min_priority=0.0,
                include_synthetic=True,
                after_id=None,
                limit=1,
                keyword_ids=[kw.id],
            )[0]
            regions = ["WORLD"] if proxy else list(self._config.regions)
            languages: list[str | None] = [None] if proxy else list(self._config.languages)
            for region in regions:
                for language in languages:
                    self._ensure_target(repo, view, region, language, choice, proxy, now)
            targets = [
                t for t in repo.targets(query_text=canonical, limit=100) if t.is_proxy == proxy
            ]
            messages = []
            ok = False
            for target in targets:
                if (target.manual_override or {}).get("backfill_only"):
                    target.manual_override = {
                        k: v for k, v in target.manual_override.items() if k != "backfill_only"
                    }
                if target.status is M.DISABLED:
                    set_status(
                        repo,
                        target,
                        M.PENDING,
                        reason="manually re-enabled",
                        actor=MANUAL_ACTOR,
                        now=now,
                    )
                    target.manual_override = {
                        k: v for k, v in (target.manual_override or {}).items() if k != "excluded"
                    }
                usable = self._usable_provider(target)
                if usable is None:
                    set_status(
                        repo,
                        target,
                        M.PROVIDER_UNAVAILABLE,
                        reason="no available provider: " + "; ".join(choice.reasons),
                        actor=MANUAL_ACTOR,
                        now=now,
                    )
                    messages.append(
                        f"{target.measurement_category.value}/{target.region}: no available "
                        f"provider ({'; '.join(choice.reasons)}); not monitored"
                    )
                    continue
                target.provider_in_use = usable
                try:
                    set_status(
                        repo,
                        target,
                        M.ACTIVE,
                        reason="manually activated",
                        actor=MANUAL_ACTOR,
                        now=now,
                    )
                except InvalidMonitoringTransitionError as exc:
                    messages.append(str(exc))
                    continue
                target.next_collection_at = now
                ok = True
                messages.append(
                    f"{target.measurement_category.value}/{target.region}: active via {usable}"
                    + (" (PROXY monitoring)" if target.is_proxy else "")
                )
            self._sync_keyword_status(session, repo, SyncReport(), now)
            return ok, "; ".join(messages) or "no targets"

    def set_manual_status(self, query: str, status: MonitoringStatus, reason: str) -> list[str]:
        now = self._clock()
        out = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            jobs = JobRepository(session)
            for target in self._resolve(repo, query):
                if status is M.DISABLED:
                    target.manual_override = {**(target.manual_override or {}), "excluded": True}
                try:
                    changed = set_status(
                        repo, target, status, reason=reason, actor=MANUAL_ACTOR, now=now
                    )
                except InvalidMonitoringTransitionError as exc:
                    out.append(f"target {target.id}: {exc}")
                    continue
                if status in (M.PAUSED, M.DISABLED):
                    cancelled = jobs.cancel_for_target(target.id, now, f"target {status.value}")
                    out.append(
                        f"target {target.id}: {status.value} ({cancelled} queued job(s) cancelled)"
                        if changed
                        else f"target {target.id}: unchanged"
                    )
                else:
                    target.next_collection_at = now
                    out.append(
                        f"target {target.id}: {status.value}"
                        if changed
                        else f"target {target.id}: unchanged"
                    )
            self._sync_keyword_status(session, repo, SyncReport(), now)
        return out

    def set_override(self, query: str, **override: object) -> list[int]:
        now = self._clock()
        ids = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            for target in self._resolve(repo, query):
                merged = {**(target.manual_override or {})}
                for key, value in override.items():
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                target.manual_override = merged
                repo.add_event(
                    target.id,
                    kind="override",
                    from_value=None,
                    to_value=",".join(sorted(merged)) or "none",
                    reason=f"manual override {override}",
                    actor=MANUAL_ACTOR,
                    at=now,
                )
                if "force_at" in override:
                    target.next_collection_at = now
                ids.append(target.id)
        return ids
