# Phase 4 architecture — statistical surge detection

Phase 4 reads the quality-controlled time series that Phase 3 stores and decides, causally
and explainably, whether a monitored query shows an **abnormal or sustained upward change**
in what its source measures. It produces persistent, deduplicated **surge episodes** with a
typed, versioned output contract for Phase 5. It does not analyse search intent or content
gaps, forecast, or recommend videos.

## 1. Audit (before implementation)

| Area | Finding | Consequence for Phase 4 |
|---|---|---|
| Observations | `ts_series` (one scale per series) and `ts_observations` (append-only revisions, one current row per window, `NULL` never zero, publication/collection/ingestion times) | detection works **per series**; it never mixes series |
| Per-request scales | `RELATIVE_PER_REQUEST` sources (mock, website-style Trends) create one series per request batch | history for such a series is one request window; baselines never cross batches |
| Measurement categories | Phase 3 `MeasurementCategory` (7 categories) plus `simulated_category` on synthetic rows and an `is_proxy` flag | evidence classification derives from these; nothing is re-labelled |
| Availability | `monitoring/replay.py::availability` (provider publication time, else conservative `max(collected, ingested)`) | detection replay reuses it unchanged |
| Comparability | `monitoring/comparability.py` (status + reason codes, series signature) | preprocessing uses it as a guard; mixed inputs are rejected |
| Readiness | Phase 3 profiles count usable history (14 / 56 daily values) | detection has its own, stricter baseline readiness per model |
| Retention | observations carry `expires_at` from versioned data policies; synthetic never expires; Google Trends denied until a retention period is configured; YouTube derived metrics disabled | every derived detection row carries the earliest expiry of its sources and is purged with them |
| Integration point | the Phase 3 executor stores a job's observations in one transaction | a transactional outbox row (+ `NOTIFY`) is written in that transaction |
| Discovery context | keywords carry novelty kind, entity type/first-seen, topic cluster, linked discovery evidence | cold-start corroboration reads these; they never become search-interest values |
| Data available here | no Google Trends access, no YouTube search-interest source; synthetic providers, the Phase 1 mock, proxy series from discovery evidence | real-world accuracy cannot be measured; real calibration impossible here |
| Dependencies | NumPy/SciPy were not dependencies | added (`numpy>=2.0`, `scipy>=1.13`; `scipy-stubs` for mypy) |
| Tests | 437 passed, 3 live tests deselected | regression baseline |

No blocking defects were found in Phases 1–3. Small additive changes: `StoredPoint`
exposes `expires_at`; the executor writes the detection outbox; `MonitoringService` takes
an optional `after_collection` hook; retention covers detection rows.

## 2. Modules

```
src/trend_agent/
  schemas/detection.py   detector / baseline / readiness / support / run vocabulary, reasons
  schemas/evidence.py    evidence classification, stage ceilings, validation rules
  schemas/surge.py       versioned SurgeAlert / TargetSurgeView output contract (Phase 5)
  detection/
    config.py            config/detection.yaml (all parameters; digest = version tag)
    preprocessing.py     knowledge-at-T, comparability, period grid, quality, status
    baseline.py          robust rolling + seasonal profile baselines, model selection
    statistical_tests.py vectorised NaN medians/MAD, Kruskal-Wallis, logsumexp
    anomaly.py           rapid upward anomaly detector
    cusum.py             one-sided CUSUM (persisted state, ARL approximation)
    change_points.py     Bayesian online change-point detection (persisted state)
    cold_start.py        cold-start states and corroboration
    engine.py            per-series engine: incremental == recomputed, out-of-order policy
    significance.py      calibrated null tails (+GPD), BH/BY, alert budget
    severity.py          transparent severity components
    alert_policy.py      deterministic decision table -> stage, confidence, reasons
    deduplication.py     episode identity
    events.py            episode lifecycle (EpisodeStore protocol, memory store)
    explanations.py      deterministic explanation templates, limitations
    contract.py          SurgeAlert builder / renderer
    service.py           live runs, queue processing, concurrency, Phase 5 feed
    worker.py            LISTEN/NOTIFY worker on the outbox
    replay_adapter.py    causal detection replay (step-wise and fast)
    calibration.py       null tails, GPD check, CUSUM ARL tables (training split)
    evaluation.py        independent ground-truth metrics, synthetic benchmark
    synthetic.py         deterministic generator with ground-truth labels
    insights.py          read models for the CLI
    demo.py              end-to-end synthetic demo, scenarios A-N
  db/detection_models.py            Phase 4 tables (migration 0004)
  db/repositories/detector_state.py advisory lock + optimistic state versions
  db/repositories/detection_runs.py runs, results, calibrations, evaluations, outbox queue
  db/repositories/surge_events.py   SqlEpisodeStore + read queries
  cli_detect.py                     `trend-agent detect ...`
config/detection.yaml
scripts/benchmark_detection.py
```

## 3. Data flow and integration with Phase 3

```
Phase 3 executor ──(one transaction)──► ts_observations + detection_queue upsert + NOTIFY
                                                 │
        ┌────────────────────────────────────────┤
        ▼                                        ▼
 monitor run --once (after_collection hook)   detect worker (LISTEN, fallback re-check)
 detect run [--all|--category|--query]        (several workers: SKIP LOCKED claims)
        │
        ▼  DetectionService.run
 1. select series (scope or claimed queue rows)
 2. read: knowledge at T (agent availability) → preprocess → load state → engine
          (incremental, or recompute on revision / late arrival / config change)
 3. one multiple-testing family per run (calibrated p-values; BH/BY) + 24 h alert budget
 4. write per series (one transaction): advisory lock, optimistic state version,
    detector_states, anomaly_results (idempotent), decisions → surge episodes
    (advisory lock per episode key), queue entry cleared unless re-marked
 5. stale episodes resolved; detection_runs row with counts, family, budget, errors
        │
        ▼
 surge_events (+ transitions, evidence)  ──►  detect alerts / explain / Phase 5 feed()
```

Processing is idempotent (unique results per observation revision, detector, algorithm
and configuration version; episodes remember applied observation revisions), recoverable
(the outbox row survives crashes; leases expire), deterministic (same data + config →
same results) and safe under concurrent workers (advisory locks, optimistic versions,
partial unique index on open episodes). Event time (observation windows) and processing
time (`evaluated_at`, `first_detected_at`) are stored separately; the detector never sees
an observation before it was available.

## 4. Data model (migration `0004`, additive)

| Table | Purpose |
|---|---|
| `detection_runs` | one row per run: type, scope, as-of, versions, counts (series, observations, hypotheses, candidates, accepted, alerts), multiple-testing family and budget, errors |
| `detector_states` | per series and detector type (engine, anomaly baseline cache, cusum, change point, cold start): payload, watermark, last processed observation/period, versions, optimistic `version` |
| `anomaly_results` | per observation revision and detector: baseline model/value/dispersion, observed, score, threshold, calibrated p-value, quality, status, support, reasons, details, availability and evaluation time, late flag, versions, expiry; unique (observation, detector, algorithm, configuration) |
| `surge_events` | episodes: key, target, series, query, keyword, topic cluster, category, provider, measurement, classification, region, language, stage, confidence, episode start, first detected, updated, resolved, severity, maximum, peak, frozen baseline, counters, explanation, contract payload, versions, synthetic, expiry; partial unique index on open episodes; CHECK that CONFIRMED is direct |
| `surge_event_transitions` | created / escalated / downgraded / resolved / reopened / rejected with reason, severity, notification flag |
| `surge_evidence` | links to observations and detector results with contribution |
| `detection_evaluations` | evaluation runs: dataset, split, versions, precision, recall, false-alert rate, delay, duplication, coverage, severity correlation, metadata |
| `detection_calibrations` | null tails / GPD parameters per stratum, version, source, training range |
| `detection_queue` | outbox: series with new or revised observations |

## 5. Design decisions

* **Per-series engine, pure and deterministic.** All statistics are functions of the
  available data and the configuration, so incremental processing, recomputation after
  out-of-order data, live runs and replay share one code path and give identical
  results.
* **Event-driven without new infrastructure.** PostgreSQL outbox + `LISTEN/NOTIFY`; no
  broker. The fallback re-check means a missed notification only delays processing.
* **No manufactured p-values.** Scores become p-values only through a stored calibration
  of the same data kind; otherwise alerts are explicitly empirical and cannot be
  confirmed.
* **Evidence ceilings in three places**: alert policy, output contract validator and a
  database CHECK constraint.
* **Synthetic isolation**: synthetic data never mixes with real data (separate
  calibrations, flags on every row, production listings exclude it).
