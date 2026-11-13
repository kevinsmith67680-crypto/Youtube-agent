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
| Measurement categories | Phase 3 `MeasurementCategory` (7 categories) plus `simulated_category` on synthetic rows and an `is_proxy` flag | evidence classification is derived from these; nothing is re-labelled |
| Availability | `monitoring/replay.py::availability` (provider publication time, else conservative `max(collected, ingested)`) | detection replay reuses it unchanged |
| Comparability | `monitoring/comparability.py` (status + reason codes, series signature) | preprocessing runs it as a guard; mixed inputs are rejected |
| Readiness | Phase 3 profiles count usable history (14 / 56 daily values) | detection has its own, stricter baseline readiness per model |
| Retention | observations carry `expires_at` from versioned data policies; synthetic never expires; Google Trends is denied until a retention period is configured; YouTube derived metrics are disabled | every derived detection row carries the expiry of its source observation and is purged with it |
| Integration point | the Phase 3 executor stores a job's observations in one transaction | a transactional outbox row (+ `NOTIFY`) is written in that transaction |
| Discovery context | keywords carry novelty kind, entity type/first-seen, topic cluster, and linked discovery evidence (official announcements, releases, suggestions, community, video titles) | cold-start corroboration reads these; they never become search-interest values |
| Data actually available here | no Google Trends access, no YouTube search-interest source; only synthetic providers, the Phase 1 mock, and proxy series from discovery evidence | real-world accuracy cannot be measured; calibration on real data is impossible here |
| Dependencies | NumPy/SciPy were not dependencies | added (`numpy>=2.0`, `scipy>=1.13`) |
| Tests | 437 passed, 3 live tests deselected (baseline before Phase 4) | regression target |

No blocking defects were found in Phases 1–3.

## 2. Implementation plan

1. Vocabulary and contract (`schemas/detection.py`, `schemas/evidence.py`, `schemas/surge.py`).
2. Versioned configuration (`config/detection.yaml`, `detection/config.py`).
3. Storage: migration `0004` (detection runs, detector state, anomaly results, surge
   episodes + transitions + evidence, evaluations, calibrations, outbox queue);
   repositories.
4. Statistical core, all causal and pure: preprocessing → baselines (robust rolling,
   seasonal) → rapid anomaly → CUSUM → change point (BOCPD) → cold start.
5. Decision layer: evidence classification, severity, multiple-testing control, alert
   policy, episode management, deterministic explanations.
6. Service: live runs, event-driven queue worker, Phase 3 hook, concurrency control.
7. Replay adapter, calibration, independent evaluation, synthetic scenarios A–N, CLI, demo.
8. Tests (28 areas), benchmarks, documentation.
