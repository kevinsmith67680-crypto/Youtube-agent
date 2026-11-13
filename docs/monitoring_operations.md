# Monitoring operations

## Running locally

```bash
docker compose up -d                       # PostgreSQL 16 (dev + test databases)
pip install -e ".[dev]"                    # or: uv pip install -e ".[dev]"
trend-agent db upgrade                     # migrations up to 0003

trend-agent discover run --all             # Phase 2: candidates (optional; see README)
trend-agent monitor sync                   # select/activate targets from candidates
trend-agent monitor plan --dry-run         # what would be scheduled, and its quota cost
trend-agent monitor run --once             # sync + plan + execute eligible jobs once

# long-running (separate terminals / services; both only need PostgreSQL)
trend-agent monitor scheduler --interval 300     # sync + plan every 5 minutes
trend-agent monitor worker --interval 15         # claim and execute jobs (start several)

trend-agent monitor demo                   # end-to-end SYNTHETIC demonstration (A-J)
```

Run commands from the repository root (or set `TREND_AGENT_DISCOVERY_CONFIG_DIR` to the
directory holding `discovery.yaml` and `monitoring.yaml`). Stop the scheduler and workers
with Ctrl-C / SIGTERM; a worker that dies mid-job leaves a lease that expires after
`jobs.lease_duration`, and the job is then recovered by another worker.

## Commands

| Command | Purpose |
|---|---|
| `monitor targets [--status --category --tier --include-synthetic --json]` | targets with category, priority, measurement, provider, tier, status, next collection |
| `monitor sync` | run target selection/activation/demotion and tiering now |
| `monitor activate --query Q [--proxy]` | activate an existing keyword (only if a suitable provider is available; otherwise explains why) |
| `monitor pause/resume/disable --query Q` | manual control (queued jobs are cancelled on pause/disable) |
| `monitor override --query Q --tier critical|auto --collect-now --keep-active/--allow-demotion` | manual overrides |
| `monitor plan [--dry-run]` | planned jobs, category allocation, quota estimate |
| `monitor run --once` | one full cycle in-process |
| `monitor worker [--id --interval --once]` | worker process |
| `monitor scheduler [--interval --once]` | scheduler process |
| `monitor status [--window-hours]` | targets, queue, attempts, latency, quota, provider health, coverage |
| `monitor providers` | capabilities, cadence, delay, availability, health |
| `monitor freshness [--query]` | provider and target freshness |
| `monitor readiness [--query --profile]` | baseline readiness with reasons |
| `monitor quality [--query]` | data-quality findings (provider/category/target/series) |
| `monitor quota` | remaining budgets, resets (provider time zone), usage, deferrals |
| `monitor history --query Q [--provider --current-only]` | observations with provenance and revision lineage |
| `monitor backfill --query Q --provider P --days N [--dry-run --queue-only]` | historical backfill (resumable) |
| `monitor replay --query Q [--provider --start --end --step-hours --mode]` | replay without look-ahead |
| `monitor demo [--no-reset]` | synthetic end-to-end demonstration with scenario checks (exit 1 if any fails) |

Lists hide synthetic targets unless `--include-synthetic`; query-specific commands show
everything with labels. All commands accept `--json` where useful. Logs are JSON on stderr
with `job_id` and `provider` fields; secrets are never logged.

## What to expect in a fresh environment

With no Google Trends alpha access, there is **no real search-interest provider**. Real
keywords then get search targets in `provider_unavailable` (the reason lists each provider
and why) and, if proxy monitoring is enabled, ACTIVE **proxy** targets that collect daily
event/community/suggestion counts from stored discovery evidence. Only the demo registers
the synthetic providers.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `monitoring configuration: … not found` (exit 2) | run from the repository root or set `TREND_AGENT_DISCOVERY_CONFIG_DIR` |
| targets `provider_unavailable` | no available provider for the measurement: `monitor providers` shows why (e.g. Google Trends `not_configured`); configure access or rely on proxy targets |
| targets `pending` "waiting for monitoring capacity" | raise `capacity.max_active_targets` or lower activation; capacity follows 45/25/30 |
| targets `quota_blocked`, jobs `deferred` | local/server quota exhausted; resumes at the reset shown by `monitor quota` (provider time zone, DST-aware) |
| many `no_new_data` jobs | the source had nothing new; check `monitor freshness` (a `provider_delayed` source publishes late) |
| targets `stale`, provider `unhealthy` | repeated transient failures; see `monitor status` errors and `monitor quality`; recovers on the next success |
| jobs stuck in `claimed`/`running` | a worker died; leases expire after `lease_duration` and any worker recovers them |
| readiness `incompatible_history` | history split across normalisation contexts (per-request scaled source) or wrong granularity — by design these are not combined |
| readiness `stale_history` | the newest stored period is older than the profile allows and freshness is delayed/unavailable |
| demo checks fail on re-run | run with the default `--reset` (removes earlier synthetic monitoring rows only) |
| Google Trends data `withheld` | storage denied until `TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS` is set per your access terms |

## Retention

`trend-agent data retention [--purge]` reports and deletes expired monitoring observations
along with Phase 1/2 data. Observations expire by data policy:

* synthetic: never;
* Google Trends: only stored once a retention period is configured;
* event signals: with the earliest expiry of their source discovery items;
* YouTube: 30 days (collection disabled).

Schedule the purge daily in production.

## Benchmarks

```bash
TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/trend_agent_test \
  python scripts/benchmark_monitoring.py --json-out bench.json
```

The script **resets** the database and refuses names without `test`/`bench`. Results and
hardware are in [phase3_report.md](phase3_report.md#g-test-results-and-performance).
