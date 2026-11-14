# Cost control and optimisation

Spending is bounded by configuration, measured from what the stages record, and enforced
before money is spent.

## 1. Budgets and limits

| Setting (env `TREND_AGENT_…`) | Default | Enforced where | When exceeded |
|---|---|---|---|
| `DAILY_API_BUDGET` (units, all providers, open quota windows) | 9,000 | scheduler (capability `collect_real_data`) | collection and gap analysis are skipped; mode DEGRADED; alert |
| `DAILY_LLM_BUDGET` (estimated USD, UTC day) | 5.0 | recommendation and content-analysis LLM paths | deterministic templates / deterministic-only analysis; mode DEGRADED; alert |
| `MONTHLY_LLM_BUDGET` (estimated USD, calendar month) | 60.0 | same | same |
| `MAX_RECOMMENDATIONS_PER_RUN` | 20 | scheduled generation | the run is capped |
| `MAX_ANALYSIS_REQUESTS_PER_TOPIC` (per rolling 24 h) | 2 | content-analysis stage | the topic is skipped until the window passes |
| `MAX_RETRIES` | 3 | every provider retry policy (`min(HTTP_MAX_ATTEMPTS, MAX_RETRIES + 1)` attempts) | permanent errors are never retried |
| `MAX_CONCURRENT_WORKERS` | 4 | worker start-up | a further worker refuses to start (exit 3) |
| per-provider quotas | YouTube 10,000 units/day with a 10 % margin | provider quota store | requests stop; jobs defer until the reset |

Warnings fire at `BUDGET_WARNING_RATIO` (0.8). A limit of 0 means "spend nothing", never
"unlimited".

## 2. How spend is measured

* **LLM**: `recommendation_generation_runs.estimated_cost_usd` plus
  `content_analysis_runs.cost.llm_usd`. Both are estimates from recorded token counts and
  the configured per-token prices. **The provider's invoice is authoritative.**
* **API units**: `provider_rate_limits.units_used` over the quota windows open now. That
  is YouTube quota units, plus one unit per request for request-counted providers.

`trend-agent system costs` prints the budgets, today's breakdown by stage and provider, and
the limits. `/metrics` exports `trend_agent_budget{kind="used|limit"}`.

## 3. Reference costs (from earlier phases' measurements)

* **LLM-assisted recommendation:** about **$0.024** per recommendation and $0.081 per
  opportunity, from the Phase 7 estimate at the configured prices. The template path
  costs **$0**.
* **YouTube search:** `search.list` costs 100 units, so the default 9,000-unit budget allows
  at most about 90 searches a day, minus metadata calls. Content analysis caches results
  and re-analyses only after 72 h or on changed inputs.
* **Smoke test:** 1 unit per run (`videoCategories.list`).

## 4. Optimisation levers (in order of effect)

1. **Reuse before spending.** Unchanged evidence reuses the stored recommendation (input
   fingerprint) and the stored analysis (72 h cache).
2. **Generate less often.** The recommendations stage runs every 6 h by default; daily is
   enough for a single creator (`config/operations.yaml`).
3. **Templates first.** Keep the LLM disabled in `config/recommendations.yaml` until the
   template output has been judged insufficient for the creator.
4. **Narrow monitoring.** Fewer active targets means fewer collection jobs (Phase 3
   capacity limits).
5. **Fail cheap.** Permanent errors are not retried, invalid requests are not repeated, and
   quota exhaustion defers instead of failing.

## 5. Degraded mode is the safety net

An exhausted budget never crashes anything and never silently keeps spending:

* the capability is withdrawn;
* the mode becomes DEGRADED with the reason;
* an alert is raised once per cooldown;
* the cheaper path (templates, cached data) continues.

E2E scenarios `s11`–`s14` verify this.
