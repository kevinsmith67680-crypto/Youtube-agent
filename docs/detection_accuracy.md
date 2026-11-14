# Detection accuracy (Phase 8)

Command: `trend-agent evaluate detection [--size tiny|small|full] [--tune]`. Code:
`src/trend_agent/evaluation/detection.py`. Reports:

* `docs/benchmarks/phase8_evaluation.json` (`results.detection`);
* `docs/benchmarks/phase8_detection_tuning.json`.

## 1. Splits

Each split comes from the Phase 4 generator with disjoint seeds and topic clusters:

| Split | Series (small) | Contents | Used for |
|---|---|---|---|
| calibration | 62 | null series only (no events) | fitting the empirical null thresholds |
| dev | 120 | ~50 % with one injected event (spike, step, ramp or event) | the tuning loop, and nothing else |
| test | 200 | as dev, different seeds and clusters | one evaluation per configuration; never tuned on |

Evaluation window: days 90–150 of each 150-day series.

## 2. Metrics (synthetic, held-out test; current configuration `cusum.h = 6`, `fdr_q = 0.05`)

| Source class | Precision | Recall | F1 | False alerts / 1,000 series-day | Delay median / p90 (days) | Confirmed precision / recall |
|---|---|---|---|---|---|---|
| all (direct) | **0.906** | **1.000** | 0.951 | **0.42** | 0.5 / 6.5 | 0.974 / 0.792 |
| DIRECT_YOUTUBE_SEARCH_INTEREST | 0.906 | 1.000 | 0.951 | 0.42 | 0.5 / 6.5 | 0.974 / 0.792 |
| VIDEO_ACTIVITY (proxy) | 0.889 | 1.000 | 0.941 | 0.50 | 0.5 / 6.5 | — / **0.000** |

* **Labels and outcomes:** 48 labelled events in the test window, 48 detected, 5 false
  alerts.
* **Recall by event kind:** 1.0 for every kind (spike 13/13, step 9/9, ramp 15/15, event
  11/11).
* **False alerts by category:** ai 3, gaming 1, technology 1.
* **Dev (for reference):** precision 0.80, recall 1.0, 0.83 false alerts per 1,000
  series-day.

**Proxy sources never reach CONFIRMED.** A proxy series can raise a candidate, but the alert
policy never confirms a search-demand surge from proxy data; hence confirmed recall 0.

**Source split.** The same held-out series were replayed as each source class. The
difference therefore isolates the class-dependent policy, not differences in the data.

## 3. Optimisation loop

`--tune` searches a 3 × 2 grid (`cusum.h` ∈ {5, 6, 7}, `fdr_q` ∈ {0.05, 0.10}) **on dev only**:

1. **Fewer alerts is not better.** A candidate is eligible only if its dev false-alert rate
   is not higher than the current configuration's.
2. **One held-out comparison.** The best eligible candidate by dev F1 is compared once with
   the current configuration on test.
3. **Adoption rule.** The candidate is adopted only if test F1 improves by at least 0.01,
   recall does not fall and false alerts do not rise.
4. **Proposal only.** Adoption writes an experiment and a proposal; **the configuration file
   is never changed by the loop**. An operator applies a proposal by editing
   `config/detection.yaml` (new `threshold_version`) and re-running the evaluation.

### Latest run (2026-11-14, `--size small --tune`, experiment `40baf281-…`)

| `cusum.h` | `fdr_q` | dev precision | dev recall | dev F1 | dev FA / 1,000 series-day | eligible |
|---|---|---|---|---|---|---|
| 5 | 0.05 | 0.727 | 1.0 | 0.842 | 1.25 | no (more false alerts) |
| 5 | 0.10 | 0.706 | 1.0 | 0.828 | 1.39 | no |
| **6** | **0.05** | 0.800 | 1.0 | 0.889 | 0.83 | yes (current) |
| 6 | 0.10 | 0.750 | 1.0 | 0.857 | 1.11 | no |
| 7 | 0.05 | 0.828 | 1.0 | **0.906** | 0.69 | yes (best on dev) |
| 7 | 0.10 | 0.774 | 1.0 | 0.873 | 0.97 | no |

On the held-out test split, `h = 7` was **worse** than the current setting:

* F1 0.931 against 0.951;
* recall 0.979 against 1.000;
* 0.50 against 0.42 false alerts per 1,000 series-day.

**Decision: rejected**, and the configuration is unchanged. The dev improvement did not
generalise, which is exactly what the single held-out comparison is there to catch.

## 4. Real-world accuracy

**UNKNOWN.** There are 0 labelled real surges, against a minimum of 30. Once labels exist,
`trend-agent evaluate replay` scores stored live alerts (`surge_events.first_detected_at`,
raised in real time and therefore free of look-ahead) against them. A label matches an alert
on the same subject within ±3 days.

Phase 4's warm-up false-alert rate (4.4 per 1,000 series-day before calibration) still
applies to newly monitored series until enough history accumulates.

## 5. Limits

* The generator and the detector were designed together. Real surges may have shapes,
  noise and missing-data patterns the generator does not produce.
* All series are daily; hourly detection is supported, but this evaluation does not cover
  it.
* Real data in this deployment would be proxy-only (see `live_data_readiness.md`), so
  every real alert would be a candidate, never a confirmed YouTube Search surge.
