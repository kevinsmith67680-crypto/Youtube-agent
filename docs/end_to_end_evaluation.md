# End-to-end evaluation

`trend-agent evaluate pipeline` runs every component evaluation, the pipeline integrity
check and the backtest-validity check. It records each run in the experiment registry and
produces the quality scorecard. The latest report is
`docs/benchmarks/phase8_evaluation.json`, measured on 2026-11-14 at `--size small
--forecast-scale 0.4`, in 2.2 minutes on 4 CPUs.

## 1. Principles

* **Origin is explicit.** Every result says `synthetic` or `real`. Synthetic results show
  that the pipeline behaves as designed. They are never reported as real-world accuracy.
* **Real-world accuracy is UNKNOWN until labelled reality exists.** Minimum real labels per
  component:
  * 30 surge events;
  * 50 content gaps;
  * 100 forecast outcomes.

  There are **0** today.
* **No tuning on the held-out split.** Detection tunes on dev only and compares once on
  test. Content-gap thresholds were swept on the calibration split in Phase 5. Phase 7
  duplicate thresholds were chosen on calibration.
* **No look-ahead.** A label carries `known_at`, and evaluations use it only from then on.
  Backtests reconstruct availability (§4).
* **No fabrication downstream.** The integrity check fails if real records derive from
  synthetic evidence. Several such contaminations are also impossible by schema constraint
  (E2E `s34`).

## 2. Results (synthetic / fixture data)

| Component | Data | Key results (held-out) | Offline target | Real world |
|---|---|---|---|---|
| Detection | 200 synthetic series (test), calibrated on 62 null series, dev 120 | precision **0.906**, recall **1.00**, F1 0.951, false alerts **0.42 / 1,000 series-day**, delay median 0.5 d, p90 6.5 d | P ≥ 0.6, R ≥ 0.6, FA ≤ 5 | UNKNOWN (0 labels) |
| Forecasting | Phase 6 synthetic corpus at scale 0.4 | 5 of 18 scopes have a validated model (all trajectory); **5/5 beat the best simple baseline** (MAE −14 % to −28 %); every breakout/continuation scope falls back to baselines at this scale; 6 h from daily data is **UNSUPPORTED** | beats baseline share ≥ 0.5 | UNKNOWN (0 outcomes) |
| Content gaps | 12 held-out fixture cases (author-labelled) | false gap rate **0.0**, false VERIFIED_GAP **0**, gap precision 1.0, recall 0.8 | false gap rate ≤ 0.25, false verified = 0 | UNKNOWN (0 labels) |
| Recommendations | 8 held-out fixture drafts; 17 generated ideas | status accuracy 1.0, false approvals **0**, unsupported statements published **0**, duplicate F1 **0.889**, top-10 max title Jaccard 0.5 | grounding = 0, dup F1 ≥ 0.8, false approvals = 0 | DESCRIPTIVE only once outcomes exist |
| Pipeline integrity | development database | **PASS** (no cross-origin derivation); real pipeline not exercised (0 real observations) | PASS | — |
| Backtest validity | development database | 0 real series (nothing to replay) | — | — |

Experiment IDs are in the report (`experiments`). Per-component detail is in
`detection_accuracy.md`, `forecast_validation.md`, `content_gap_accuracy.md` and
`recommendation_quality.md`.

### What these numbers do *not* show

* **The synthetic generators were written by the same team as the detectors.** High synthetic
  recall shows that injected patterns of the designed shapes are found. It does not show
  that real YouTube surges look like those patterns.
* **The fixture cases are small** (8–12 per split) and author-labelled. Ratios of 1.0 on
  12 cases have wide uncertainty; a 95 % Wilson interval for 12/12 is about [0.76, 1.0].
* **Outcomes:** nothing here predicts views or success. There is no claim that higher
  search interest yields more views.

## 3. Ground truth

```
trend-agent evaluate labels import labels.jsonl --origin real    # or synthetic
trend-agent evaluate labels stats
```

Each line needs:

* `kind`;
* `subject_key` (e.g. `target:42`);
* `event_time` and `known_at` (RFC 3339 with offset; `known_at ≥ event_time`);
* `source` and `labeler` (provenance);
* an optional `label` object.

A file has exactly one origin. Real labels cannot name synthetic subjects. Duplicates are
ignored and counted. Invalid lines are rejected with a reason and never stored (E2E `s35`).

Suggested real labelling sources, all entered by a person:

* the creator's own YouTube Studio search-term reports for their channel;
* announcements with known release dates;
* manual review of surge candidates after the fact.

A label from a proxy signal is still a proxy label and must say so in `source`.

## 4. Walk-forward backtests

`trend-agent evaluate replay` classifies every stored real series:

| Class | Meaning |
|---|---|
| **VALID** | collected live, so replay sees what was known |
| **LIMITED** | ≤ 50 % backfilled; early steps see less than a live system would have |
| **INVALID** | mostly backfilled or missing timestamps; do not report accuracy |

Availability uses `provider_published_at`, falling back to `max(collected_at, ingested_at)`.
The availability-aware detection replay itself is `trend-agent detect replay`. Live alerts
stored as `surge_events.first_detected_at` were raised in real time, so they are free of
look-ahead and are what `score_real` compares with real labels.

## 5. Reproduce

```
trend-agent evaluate pipeline --size small --forecast-scale 0.4 --out docs/benchmarks/phase8_evaluation.json
trend-agent evaluate detection --tune --out docs/benchmarks/phase8_detection_tuning.json
trend-agent evaluate forecasting --scale 1.0         # the Phase 6 scale (about 3 min)
trend-agent evaluate content-gaps
trend-agent evaluate recommendations
```
