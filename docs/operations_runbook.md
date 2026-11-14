# Operations runbook

For the person on call. Every command runs inside a backend container, or anywhere the
`trend-agent` CLI is installed with the production environment.

## 1. Daily check (2 minutes)

```
trend-agent system health          # exit 1 if anything fails
trend-agent system providers       # readiness + operating mode
trend-agent system costs           # spend vs budgets
trend-agent system alerts          # open operational alerts
trend-agent system dead-letters    # unresolved permanent failures
```

## 2. Alerts and what to do

Alerts are deduplicated by fingerprint. A notification is sent on the first occurrence, on
reopening, and then at most once per `OPS_ALERT_COOLDOWN_MINUTES` (default 60) while the
alert keeps recurring. Resolved conditions close their alerts automatically.

| Alert kind | Meaning | Action |
|---|---|---|
| `stage_failed:<task>` (warning → critical after 3) | a scheduled stage raised or timed out | `trend-agent system alerts --json` for the error; logs filtered by `correlation_id`; fix the cause; the stage retries with backoff and the alert resolves on success |
| `workers_missing` | no live worker heartbeat for 10 min | check the worker containers (`docker compose ps`, logs); restart; check `MAX_CONCURRENT_WORKERS` |
| `budget:<name>:WARNING/EXHAUSTED` | spend ≥ 80 % / ≥ 100 % of a budget | review `system costs`; raise the budget deliberately or wait for the window; exhaustion already degrades the mode safely |
| `mode_change` | the operating mode changed | read the reasons; a downgrade that withdraws direct-search claims needs the provider fixed before claims return |
| `dead_letters:open` | permanently failed work exists | `system dead-letters`; fix the cause (credentials, capability, malformed source); `--resolve ID --note "…"` |

## 3. Common incidents

**API not ready (`/health/ready` 503).**

1. Read `trend-agent system health`.
2. If the database is unreachable, check the managed database and the network.
3. If the schema revision is behind, run the `migrate` service. Never run the API against an
   unknown revision.

**Scheduler not running (mode DEGRADED, `scheduler_not_running`).**

1. `docker compose ps scheduler`, then read its logs.
2. Only one scheduler holds the `pipeline` lease (`/health/workers` shows the owner and
   expiry). A standby takes over automatically about 2 minutes after the leader dies (lease
   TTL).
3. Runs left `running` by a crash are closed as `abandoned` by the next leader.

**Provider failing (`provider_failing`).**

1. `trend-agent system smoke-test -p <name>`.
2. On 401/403, check the key and its restrictions.
3. On quota exhaustion, wait for the reset (YouTube: midnight Pacific).
4. Never add keys or projects to bypass quota.
5. Permanent authorisation errors are not retried, by design.

**LLM budget exhausted.** Nothing breaks: generation uses the deterministic templates. To
restore LLM generation earlier, raise `TREND_AGENT_DAILY_LLM_BUDGET` deliberately and
restart.

**Disk or database growth.** The retention stage runs daily. Check its last run with
`/metrics` (`trend_agent_stage_last_run_timestamp{task="retention"}`) or
`scheduled_runs`.

## 4. Graceful restarts and scaling

* **Graceful stop:** `docker compose stop` sends SIGTERM. Each process finishes its current
  tick, marks its heartbeat `stopped`, releases the lease (scheduler) and exits within
  `stop_grace_period`.
* **Scale workers:**
  `docker compose -f docker-compose.prod.yml up -d --scale worker=N` (N ≤ `MAX_CONCURRENT_WORKERS`).
* **Never** run two schedulers expecting both to work: the second is a standby by design.

## 5. Monitoring

Scrape `/metrics` with the API token (bearer). Useful expressions:

| Metric | Meaning |
|---|---|
| `trend_agent_operating_mode` | the current mode (label) |
| `trend_agent_heartbeat_age_seconds` | heartbeat age; > 3× interval means stale |
| `trend_agent_collection_jobs{status="failed"}`, `trend_agent_detection_queue` | queue health |
| `trend_agent_budget{kind="used"} / trend_agent_budget{kind="limit"}` | budget use |
| `rate(trend_agent_http_requests_total{status=~"5.."}[5m])` | API server errors |

## 6. Backups

See `backup_recovery.md`. Run the restore drill monthly; readiness fails after 30 days
without a successful drill.

## 7. Escalation

**Risk to quotas or credentials.** Rotate the credential, then audit:

* `provider_checks`;
* `scheduled_runs` errors;
* `ops_alerts` details.

All stored errors are redacted.

**Suspected data contamination.** Run `trend-agent evaluate pipeline`. Its integrity check
reports any real record derived from synthetic evidence.
