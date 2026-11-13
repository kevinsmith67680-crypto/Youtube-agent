# Monitoring scheduler

## Targets and activation

`TargetManager.sync()` considers keywords whose discovery status is PRIORITIZED (or
already MONITORING) and whose discovery priority is at least `activation.min_priority`
(55). For each it ensures one search-interest target per configured region/language and,
when proxy monitoring is enabled, one proxy target. A target becomes **ACTIVE** only if:

1. **provider capability** — a provider that measures the preferred category
   (`measurement_preferences`: YouTube search interest, then external search interest) is
   available. Synthetic keywords only use synthetic providers; real keywords never do.
   A transient outage does not switch a keyword to another measurement;
2. **data quality** — the keyword has ≥ `min_independent_origins` independent origins;
3. **capacity** — the category has room: `max_active_targets × share` (45/25/30), unused
   share redistributed to categories with more candidates, `exploration_share` (20 %)
   reserved for new subjects (novelty `new_*`, entity first seen within 7 days).

Otherwise the target stays **PENDING** ("waiting for monitoring capacity") or
**PROVIDER_UNAVAILABLE** (with each provider's status in the reason). Such keywords are
never presented as being under search-demand monitoring.

**Demotion** (ACTIVE → DORMANT): after `min_active` (7 days), when discovery priority
falls below `deactivate_below` (40; hysteresis gap to 55) or there has been no new
discovery evidence for `dormant_after_no_evidence` (30 days). Time-sensitive targets are
not demoted while their event is running. **Re-activation** requires the
`reactivation_cooldown` (3 days). Rejected keywords → DISABLED. Manual: `pause`, `resume`,
`disable` (excluded until `activate`), `override --tier/--collect-now/--keep-active`.

Monitoring states: PENDING, ACTIVE, PAUSED, PROVIDER_UNAVAILABLE, QUOTA_BLOCKED, STALE,
DORMANT, DISABLED. Allowed transitions are in `monitoring/lifecycle.py`; only manual
actions pause/resume or re-enable a disabled target. Every change is logged in
`monitoring_target_events` with reason and actor (target_manager, planner, executor,
manual).

## Tiers (operational importance, never "surging")

| Tier | Rule (defaults) | Re-check after "no new data" | Periods per request |
|---|---|---|---|
| CRITICAL | priority ≥ 75, new subject, time-sensitive entity (AI model/product, game event/update, release) first seen ≤ 14 days ago or event still running | 2 h | 1 |
| HIGH | priority ≥ 65 and new subject ≤ 30 days old, or priority ≥ 75 | 4 h | 1 |
| NORMAL | priority ≥ 50 | 12 h | 1 |
| LOW | evergreen subjects or lower priority | 24 h | 7 |
| DORMANT | dormant targets | 7 days | 28 |

Tiers are raised immediately and lowered only after the current tier has held for
`downgrade_hold` (24 h), so a target does not thrash. No surge probability is used:
Phase 4 does not exist yet.

## Cadence: never poll faster than the source publishes

For a target holding data up to period end `E`, with provider granularity `g`, publication
cadence `c`, tier `collect_every_periods = k` and estimated publication delay `d`:

```
next useful check = E + g·max(1, c/g)·k + d
```

Before that moment a request can only return "no new data", so none is scheduled (the
planner counts `skipped_no_new_data`). After a check that found nothing new, the next
check waits the tier's re-check interval. The provider's `min_poll_interval` can only
delay a check, never bring it forward. Polling a daily source every five minutes would
still produce one value per day; the unit test
`test_polling_every_five_minutes_cannot_create_finer_data` demonstrates this. Irregular
sources (event signals) are checked on the tier's interval.

The delay `d` comes from the [freshness estimator](time_series_data.md#freshness): the
median of observed `published − period_end` when providers state publication times,
otherwise the declared profile delay (collection-time upper bounds never shorten it).

## Planning cycle and category allocation

1. Due targets: status ACTIVE/STALE/QUOTA_BLOCKED and `next_collection_at ≤ now`
   (paginated). Targets with an open job are skipped (no duplicate planning).
2. Provider check; unavailable → PROVIDER_UNAVAILABLE (no request). Transient
   unavailability is also recorded as a provider-health failure.
3. Cadence check (above).
4. Score = tier weight × (0.5 + priority/100) × aging (1 + overdue days, max 4). Targets
   overdue by `starvation_after` (72 h) jump the queue.
5. Capacity `min(max_jobs_per_cycle, max_queue_depth − queue depth)` is split by category
   (largest remainder over 45/25/30). Within each category `exploration_share` (20 %) is
   reserved for exploratory targets (no data yet, or activated within 7 days). Unused
   slots are redistributed to the best remaining targets of any category, so no category
   starves while capacity is idle, and nothing is invented to fill a share. Actual usage
   per category is stored in `monitoring_plan_runs`.
6. Quota (below), then the job is created with idempotency key
   `type:target:provider:expected-period:hour`.

`monitor plan --dry-run` runs every read and decision in a transaction that is rolled back.

## Quota-aware scheduling

Before a job is created the planner estimates provider, endpoint (request), cost
(`quota.request_cost`), remaining quota (Phase 1 `QuotaManager`, safety margin included),
already-queued cost and the reset time. If the cost does not fit, the target becomes
QUOTA_BLOCKED with `next_collection_at` = the reset. The executor reserves quota again
before every request (atomic, persisted in `provider_rate_limits`). Server-side or local
exhaustion → the job is **DEFERRED** until `resets_at` (not counted as a failed attempt).

Resets follow each provider's documented time zone with DST (e.g. YouTube and the
synthetic provider: midnight America/Los_Angeles = 08:00 UTC in winter, 07:00 UTC in
summer; the demo crosses 2026-03-08 and shows both). Defaults for the YouTube Data API
(100 `search.list` calls/day + 10,000 units/day for other endpoints) are configurable in
settings. Quotas are never bypassed, and keys are never rotated to evade limits. Tracked:
estimated and actual (ledger) usage per provider, remaining budget, deferred jobs, quota
incidents per plan, and skipped "no new data" checks.

## Job queue, leases and recovery

| Status | Meaning |
|---|---|
| PENDING | waiting for its scheduled time |
| CLAIMED / RUNNING | leased by a worker (`lease_owner`, `lease_token`, `lease_expires_at`) |
| SUCCEEDED | new or revised observations stored |
| NO_NEW_DATA | request succeeded, nothing new: **not** a data refresh |
| RETRY_WAIT | transient failure; `retry_after` = exponential backoff with full jitter, at least the provider's Retry-After |
| DEFERRED | quota exhausted until reset |
| FAILED | permanent (authorization, capability, validation) or max attempts |
| CANCELLED | target paused/disabled before execution |

* **Claiming**: `FOR UPDATE SKIP LOCKED`; a fresh `gen_random_uuid()` lease token per claim.
  The integration test runs four threads against PostgreSQL: every job is claimed exactly once.
* **Bounded concurrency**: `worker_concurrency` jobs in flight per worker (semaphore).
* **Leases**: `lease_duration` (5 min); long collections renew in the background.
* **Recovery**: workers first return expired leases to RETRY_WAIT (or FAILED after
  `max_attempts`), recording the abandoned attempt as `lease_expired`.
* **Fencing**: every state change after the claim requires the claim's token, so a worker
  that lost its lease cannot overwrite the new owner's result.
* **Idempotency**: re-executing a job (e.g. crash after writing) stores no duplicates: the
  rewrite reports `unchanged` and the job ends NO_NEW_DATA.
* **Retries**: transient errors and storage failures retry with backoff; rate limits
  respect Retry-After (but a Retry-After beyond the retry cap is not slept on); permanent
  errors fail at once and set the target PROVIDER_UNAVAILABLE; `stale_after_failures`
  consecutive transient failures set the target STALE; `unhealthy_after_failures` make the
  provider UNHEALTHY. Success restores ACTIVE.
