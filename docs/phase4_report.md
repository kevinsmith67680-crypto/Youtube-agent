# Phase 4 report — Statistical Surge Detection Engine

Date: 2026-11-13. Branch `claude/tender-thompson-620dmi`. Migration head `0004`.
Configuration `detection-v1`, algorithm `detect-1.0.0`, thresholds
`thresholds-2026-11-13-synthetic`.

> Everything measured here is **synthetic**. No real search-interest source is available
> in this environment (Google Trends API alpha not configured; the YouTube Data API has no
> search interest). Real-world precision and recall are **unknown**.

## 1. Implemented components

| Component | Module | Notes |
|---|---|---|
| Evidence classification and stage ceilings | `schemas/evidence.py` | only direct YouTube search interest can be CONFIRMED (policy, contract validator, DB check) |
| Causal preprocessing | `detection/preprocessing.py` | knowledge at T, comparability guard, period grid, missing ≠ zero, partial/low-volume/scale checks, readiness statuses |
| Robust rolling baseline | `detection/baseline.py` | median/MAD of prior values, zero-MAD fallbacks flagged unstable, uncertainty intervals |
| Seasonal baseline | `detection/baseline.py` | weekday / hour-of-day profile, weekly refit, Kruskal–Wallis + one-step error gate |
| Rapid anomaly detector | `detection/anomaly.py` | upward robust z, stratified thresholds, effect/level floors, calibrated p-values only |
| CUSUM | `detection/cusum.py` | clipped one-sided CUSUM, gap reset, refractory, persisted state, ARL tables |
| Change points (BOCPD) | `detection/change_points.py` | Normal–Gamma/Student-t, truncated run length, exact state serialisation |
| Cold start | `detection/cold_start.py` | five states, corroboration from discovery evidence, low confidence, never CONFIRMED |
| Engine | `detection/engine.py` | incremental == recomputed; recomputation on revision / late arrival / config change |
| Multiple testing | `detection/significance.py`, `calibration.py` | empirical null + GPD tail, per-run BH/BY, alert budget |
| Severity | `detection/severity.py` | versioned components, correlated detectors not summed |
| Alert policy | `detection/alert_policy.py` | deterministic decision table with machine-readable reasons |
| Episodes | `detection/events.py`, `deduplication.py` | create / update / escalate / downgrade (hysteresis) / resolve / reject / reopen / cooldown, idempotent |
| Contract and explanations | `schemas/surge.py`, `detection/contract.py`, `explanations.py` | `surge-alert/1.0`, deterministic templates |
| Live service, worker, Phase 3 hook | `detection/service.py`, `worker.py`, `monitoring/executor.py` | outbox + `LISTEN/NOTIFY`, advisory locks, optimistic state versions |
| Replay, calibration, evaluation | `replay_adapter.py`, `calibration.py`, `evaluation.py`, `synthetic.py` | causal step-wise and fast replay; independent ground truth |
| Storage | `db/detection_models.py`, migration `0004`, repositories | 9 tables, idempotency constraints, retention |
| CLI | `cli_detect.py` | `detect run / worker / query / alerts / explain / readiness / thresholds / stats / schema / replay / calibrate / evaluate / demo` |

Changes to earlier phases (additive): `StoredPoint.expires_at`; the executor writes the
detection outbox; `MonitoringService(after_collection=...)` hook and
`RunOnceReport.detection`; retention purges detection rows; NumPy/SciPy dependencies;
version 0.4.0.

## 2. Synthetic demonstration (`trend-agent detect demo`)

Fictional subjects, simulated daily clock (2026-07-06 → 2026-10-30), values available
36 h after their day ends, observations written through `TimeSeriesRepository` + the
detection outbox (as the Phase 3 executor does), live detection from the queue at 12:00
each simulated day (35 runs: one over 78 days of history, then daily), calibration on a
disjoint synthetic training split. Run on 2026-11-13 with `trend-agent detect demo`:
**33/33 checks passed** (90 s). Each value is first visible 36 h after its day ends
(e.g. day 10-02 at 10-04 12:00), so every alert below fired at the first daily decision
after the onset value was published.

| Sc. | Query (fictional) | Cat. | Classification | Final status | Confidence | Max severity | First trigger | Episode start → first alert |
|---|---|---|---|---|---|---|---|---|
| A | nimbus coder 3 tutorial (breakout, +42 on base 18) | ai | direct_search_surge | **confirmed** | statistically supported | 98.2 | robust anomaly | 10-02 → 10-04 12:00 (CONFIRMED 10-05) |
| B | ai agent framework comparison (25-day ramp) | ai | direct_search_surge | **confirmed** | statistically supported | 88.0 | **CUSUM** | 09-26 → 10-02 12:00 (WATCH from 10-01) |
| C | fortnite starfall rift event (3-day event) | gaming | direct_search_surge | **resolved** | statistically supported | 98.4 | robust anomaly | 10-06 → 10-08 12:00 (CONFIRMED 10-09) |
| D | python async tutorial (weekends +60 %) | technology | – | no episode | – | – | – | seasonal profile selected |
| E | 12 steady noise series | mixed | direct_search_surge | 2 WATCH-only episodes, 0 alerts | empirically thresholded | ≤ 36.9 | – | – |
| F | lumen 7 local install (5 rising values, new entity) | ai | direct_search_surge | **watch** | **low confidence cold start** | 28.6 | cold start | – |
| G | lattice 6 upgrade guide (provider rescales ×1.8) | technology | – | no episode | – | – | – | new series, cold start |
| H | aurora 2 benchmark (external search step) | ai | **external_search_surge** | candidate (cap) | empirically thresholded | 94.4 | robust anomaly | 10-09 → 10-11 12:00 |
| I | dragonfall season 3 trailer (video spike) | gaming | **proxy_attention_signal** | resolved | empirically thresholded | 100.0 | robust anomaly | 10-10 → 10-12 12:00 |
| J | quantum sdk release notes (spike revised 71 → 26) | technology | direct_search_surge | **rejected** (revised away) | empirically thresholded | 61.5 | robust anomaly | 10-14 → 10-16 12:00 |
| K | gpu driver rollback (30 % missing days) | technology | – | no episode | – | – | – | 33 missing, 0 zeros |
| N | vector db pricing (day 98 delivered on day 102) | technology | – | no episode | – | – | – | late arrival recomputed |

Scenario checks (all passed):

| Sc. | Check | Result |
|---|---|---|
| A | breakout detected once, anomaly detector, severity, confirmation | 1 episode; first trigger robust anomaly; severity 98.2; CONFIRMED next day, statistically supported (BH) |
| B | sustained growth found by a sustained-change detector | first alert by CUSUM (not one extreme value) |
| C | short gaming event detected and resolved | resolved after returning to the pre-episode baseline |
| D | seasonal baseline reduces predictable false alarms | weekday profile selected; 0 anomaly triggers in the final 32 days vs 10 with a robust-only baseline; 0 alerts |
| E | noise | 0 alerting episodes across 12 noise series × 32 days |
| F | cold start | states early_interest_observed → cold_start_growth_candidate; WATCH, low confidence, never confirmed |
| G | normalisation change | 2 series; mixing them → `incompatible` (`different_scale_definition`, `normalization_context_changed`); no alert |
| H | external search surge | `external_search_surge`, CANDIDATE, never CONFIRMED |
| I | video popularity spike | `proxy_attention_signal`, explanation "attention candidate only, not a search-demand surge" |
| J | data revision | lineage rev1 71 → rev2 26 kept; episode REJECTED (`supporting_value_revised_away`); replay shows CANDIDATE before the revision's publication and the rejection after it |
| K | missing periods | 33 missing values stored as NULL (generated 33), 0 zeros; no alert |
| L | 1,000 null queries (50 % correlated) | 66 false alerts in 60 days = 1.1 per 1,000 series per day (target ≤ 2) |
| M | repeated processing | re-run: results/episodes/notifications 7,437 / 9 / 8 unchanged; after deleting all detector state, 2,471 observations re-evaluated, still 7,437 / 9 / 8 |
| N | out-of-order delivery | recomputation `late_arrival`; persisted state identical to in-order processing |
| G/J | deterministic replay | decision digest `0603ba7437acee56` on two independent replays |
| contract | 9/9 payloads validate as `surge-alert/1.0`; synthetic labelled and excluded from the production feed | passed |

Run totals: 2,472 observation evaluations, 1,437 calibrated hypotheses (52 accepted by
BH), 8 notifications, 1 revision recomputation, 1 late-arrival recomputation.

## 3. Test outcomes

All run in this environment on 2026-11-13 on the final code:

| Check | Result |
|---|---|
| `ruff check`, `ruff format --check` (src, tests, scripts, migrations) | clean |
| `mypy src tests` (strict, with `scipy-stubs`) | no issues in 160 files |
| full suite, Python 3.12, `TREND_AGENT_REQUIRE_DB_TESTS=1`, with coverage | **496 passed**, 0 failed, 3 live tests deselected; total coverage 91 % |
| full suite, Python 3.13 | **496 passed**, 0 failed |
| Phase 1–3 regression (the 437 tests that existed before Phase 4) | all pass |
| `alembic check` on the dev and test databases; migration round trip `0004` | no drift; upgrade → downgrade → upgrade passes |
| `trend-agent detect demo` | 33/33 scenario checks |
| `trend-agent detect evaluate --dataset synthetic` (run twice) | identical metrics (deterministic) |

Phase 4 adds 59 tests (51 unit, 8 database integration). Coverage of the detection
modules: 84–100 % (anomaly, cold start and severity 100 %; engine 97 %; events 96 %;
service 84 %; `cli_detect.py` 70 %; worker 78 %). The tests cover all 28 required areas:

| Area | Tests |
|---|---|
| baselines, robust anomaly, zero MAD, seasonal adjustment | `test_robust_baseline_uses_prior_values_only`, `test_rapid_anomaly_*`, `test_zero_mad_*`, `test_seasonal_baseline_selected_with_enough_cycles_only`, `test_no_hour_of_day_profile_for_daily_data` |
| CUSUM updates and resets, change points | `test_cusum_update_formula_*`, `test_cusum_alarm_resets_and_refractory_*`, `test_cusum_gap_resets_statistic`, `test_bocpd_*` |
| sparse series, cold start, missing observations | `test_sparse_and_irregular_series`, `test_cold_start_states`, `test_missing_values_and_periods_are_never_zero` |
| comparability, normalisation changes | `test_mixed_normalisation_contexts_are_incompatible`, demo G |
| external vs YouTube evidence | `test_evidence_classification_rules`, `test_only_direct_evidence_can_be_confirmed_*`, `test_policy_confirms_*`, `test_contract_rejects_invalid_claims` |
| multiple testing | `test_benjamini_hochberg_and_yekutieli_match_scipy`, `test_null_distribution_p_values`, `test_alert_budget` |
| episodes: creation, deduplication, resolution, reopening, budget | `test_episode_created_once_updated_and_resolved`, `test_reopen_within_window_*`, `test_budget_holds_back_notifications`, `test_episode_keys_*` |
| revisions, out-of-order data | `test_revision_and_late_arrival_trigger_recomputation`, `test_revision_is_processed_with_recomputation`, demo J/N |
| replay and look-ahead prevention | `test_detection_replay_has_no_look_ahead`, `test_revision_does_not_rewrite_earlier_decisions_*`, `test_fast_batch_evaluation_equals_stepwise_replay` |
| independent ground truth, false-alert rate | `test_ground_truth_labels_*`, `test_evaluation_metrics_on_known_alerts`, `test_false_alert_rate_on_null_series_is_measured`, `test_calibration_*_without_test_leakage` |
| database idempotency, concurrency | `test_live_detection_from_outbox_is_idempotent_*`, `test_concurrent_detection_workers_never_duplicate` (4 real threads), `test_repeated_processing_and_lost_state_do_not_duplicate` |
| CLI | `test_cli_detect_commands` (every `detect` command), `test_cli_detect_requires_database` |
| Phase 3 integration | `test_phase3_collection_queues_series_and_triggers_detection` (executor → outbox → hook → detection) |
| retention, contract, explanations, incremental equivalence | `test_derived_detection_records_follow_source_retention`, `test_contract_payload_*`, `test_explanations_*`, `test_incremental_processing_equals_full_recomputation` |
| Phase 1–2 regression | the existing suite |

Not run: live provider tests (no Trends access; no YouTube search-interest source).

## 4. Accuracy (synthetic only)

See [detection_evaluation.md](detection_evaluation.md) for definitions and leakage
control. Held-out test split (400 series, 99 surge labels) and null scale (1,000 series):

| Set | Precision | Recall | False alerts per 1,000 series / day | Median delay | Duplicates | CONFIRMED P / R |
|---|---|---|---|---|---|---|
| test, calibrated | 0.925 | 1.00 | 0.33 | 0.5 d (p90 6.5) | 0 | 0.973 / 0.737 |
| test, no calibration | 0.884 | 1.00 | 0.54 | 0.5 d (p90 6.5) | 0 | – |
| null scale (50 % correlated) | – | – | 1.10 (warm-up: ≈ 4.4) | – | – | 1 false CONFIRMED / 60,000 series-days |

Severity vs true magnitude: Spearman 0.575 (p 4.7·10⁻¹⁰). Real-world performance:
**unknown** – there are no independently labelled real surges.

## 5. Performance benchmarks

`scripts/benchmark_detection.py`, raw output in
[benchmarks/phase4_benchmark.json](benchmarks/phase4_benchmark.json). Hardware: 4 vCPU
Intel Xeon @ 2.10 GHz, 16 GB RAM, PostgreSQL 16 in Docker on the same host, Python
3.12.3. Dataset: 1,000 active targets with 120 days of daily history (121,000
observations) plus 10,000 dormant targets; synthetic values, no calibration for this
provider (so no p-values, like real data here).

| Operation | Result |
|---|---|
| Single series, incremental update (365-day history, state restored) | p50 0.86 ms, p95 1.50 ms |
| Single series, preprocessing of 365 days | 0.68 ms |
| Single series, full recomputation of 365 days | 96 ms (263 µs per period) |
| Baseline calculation (robust + seasonal, cold cache) | 119 µs per period |
| Batch, 1,000 series × 1 new observation (memory) | 1.05 s (948 series/s) |
| Fast causal replay, 1,000 series × 120 days (decisions + episodes) | 44 s (2,711 evaluations/s) |
| Step-wise replay on the Phase 3 engine, 100 series × 60 days | 7.6 s (792 evaluations/s) |
| DB: daily incremental run, 1,000 series via the outbox | **25.4 s** (39 series/s), 0 errors |
| DB: initial run over all history, 1,000 series × 120 days | 401 s (299 observations/s; 360,000 result rows; 77 episodes) |
| DB: history retrieval per series (121 points, all revisions) | p50 4.2 ms, p95 7.0 ms |
| DB: detector state load + save (5 detector rows, optimistic version) | 5.7 ms per series |
| DB: episode creation (key lock, lookup, insert, transition) | 3.4 ms per episode |

Profiling before optimising showed per-series database round trips dominate (the
in-memory engine is ~1 ms per series per new value). Two changes were made: one
multi-row upsert for the five detector-state rows (13.4 → 5.7 ms per series; daily run
32.2 → 25.4 s) and compact result rows for historical (late) periods. The initial run is
dominated by writing one result row per detector and historical period; it happens once
per series. No distributed infrastructure is needed at this scale: the daily load for
1,000 daily series takes under 30 s on one worker, and more workers can run in parallel
(SKIP LOCKED queue, advisory locks).

## 6. Data limitations and external dependencies

* **No real search-interest data.** Google Trends API (alpha) access, credentials and a
  transport are missing; YouTube has no search-interest endpoint. Detection runs on
  proxy series from discovery evidence in a real deployment here, and on synthetic data
  for demonstration.
* **Calibration is synthetic.** Real data needs its own calibration from stored
  in-control history (`detect calibrate --dataset stored` is not implemented because no
  real history exists); until then real alerts are `EMPIRICALLY_THRESHOLDED` and can
  never be CONFIRMED (`confirm_requires_calibration`).
* Proxy event series from discovery evidence only contain days with items (unknown
  otherwise), so they are often `IRREGULAR_SAMPLING`: anomaly only, no CUSUM / change
  point.
* External dependencies for real use: Google Trends API access (or another licensed
  YouTube search-interest source) and a retention period compatible with its terms.

## 7. Known limitations and unresolved issues

1. **Warm-up false alerts.** In the first ~8 weeks of a series' history (before the
   seasonal model qualifies) the synthetic null false-alert rate was ≈ 4.4 per 1,000
   series per day, above the 2/day target; the alert budget caps notifications, and the
   steady-state rate is 1.1.
2. **Per-run FDR only.** Benjamini–Hochberg is applied within each detection run; there
   is no online FDR guarantee over time. Families mix anomaly and CUSUM hypotheses of the
   same observation (positively dependent; BH remains valid under PRDS, BY is
   available).
3. **Exchangeability assumption.** Empirical p-values assume the evaluated statistic is
   exchangeable with the null sample of its stratum; series with unusual noise
   (stronger autocorrelation, other providers) need their own strata.
4. **Recomputation scope.** Recomputation after late or revised data covers the loaded
   history window (730 daily periods); older state is not reconstructed.
5. **Initial history processing is slower than steady state** (one result row per
   detector and period); daily operation processes one new value per series.
6. **Hourly data** is supported by the code path (hour-of-day profiles, hourly
   thresholds) but no hourly source exists and it was only unit-tested.
7. Category-level priors (e.g. different thresholds for gaming events) are configurable
   but not tuned – there is no data to tune them on.

## 8. Phase 5 readiness

Phase 5 (search intent and content-gap analysis) can consume without recomputing
statistics:

* `DetectionService.feed(active=..., category=...)` → validated `SurgeAlert` objects
  (`surge-alert/1.0`, JSON schema via `trend-agent detect schema`) with classification,
  stage, confidence, severity and components, baseline, detectors, multiple testing,
  comparability, limitations, explanation and evidence provenance;
* `DetectionService.target_views()` → per-target combinations
  (`POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY` for mixed indirect evidence);
* `surge_events` / `surge_evidence` / `surge_event_transitions` for history and audit;
* topic and intent cluster ids from Phase 2 on every alert.

Blocking for real (non-synthetic) Phase 5 work: a licensed search-interest source and
its calibration. Not started, by instruction: Phase 5.

## 9. Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Phase 1–3 tests remain functional | verified | 437 pre-existing tests pass (part of 496) |
| 2 | consumes Phase 3 time-series data | verified | reads `ts_observations`; Phase 3 executor → outbox → detection test; demo writes through `TimeSeriesRepository` |
| 3 | comparisons respect provider comparability rules | verified | per-series processing; mixed contexts `INCOMPATIBLE` (unit test, demo G) |
| 4 | robust anomaly detection works | verified | unit tests; demo A, C, I |
| 5 | sustained-change detection works | verified | CUSUM unit tests; demo B (first alert by CUSUM) |
| 6 | causal change-point method | verified | BOCPD unit tests (shift found, spike/drop not); no-look-ahead replay test |
| 7 | seasonal patterns handled with enough history | verified | unit test; demo D (0 vs 10 triggers) |
| 8 | sparse keywords get cold-start treatment | verified | unit test; demo F |
| 9 | direct evidence distinct from indirect signals | verified | classification tests, contract validator, DB constraint; demo H, I |
| 10 | invalid / incomparable observations cannot confirm | verified | policy and contract tests; demo G, J |
| 11 | false-positive controls | verified (synthetic) | calibrated BH/BY + alert budget; null-scale 1.1 / 1,000 series / day |
| 12 | persistent surge episodes | verified | episode unit tests; integration tests; demo |
| 13 | duplicate processing cannot duplicate alerts | verified | idempotency unit + integration tests; demo M |
| 14 | detector states survive restarts | verified | state round trip == recomputation; lost-state reprocessing test |
| 15 | replay avoids future-information leakage | verified | replay tests; demo J |
| 16 | independent synthetic ground-truth evaluation | verified | generator labels; held-out evaluation (§4) |
| 17 | alerts include evidence and explanations | verified | `surge-alert/1.0` payload validation; `detect explain` |
| 18 | CLI commands function | verified | `test_cli_detect_commands` covers every command |
| 19 | source-specific storage restrictions enforced | verified | derived rows inherit expiry and are purged (test); no new collection; YouTube derived metrics remain disabled |
| 20 | migrations apply | verified | `0004` round trip, `alembic check` |
| 21 | automated tests pass | verified | 496 passed (3.12 and 3.13) |
| 22 | code quality checks pass | verified | ruff, format, mypy strict clean |
| 23 | Phase 5 can consume typed surge events | verified (interface) | `DetectionService.feed()` / `target_views()` return validated models; no Phase 5 consumer exists yet |

Not verifiable here: accuracy on real search-interest data (no source, no labels),
behaviour with Google Trends data (no access), and live quota-consuming tests.
