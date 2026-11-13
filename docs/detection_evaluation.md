# Detection evaluation

**All numbers in this document are SYNTHETIC.** No independently labelled real surges
exist in this environment (no Google Trends access, no YouTube search-interest source),
so **real-world precision and recall remain unknown**. Synthetic results show that the
pipeline behaves as designed on data with known ground truth; they are not a claim about
real search demand.

## Ground truth (`detection/synthetic.py`)

Series are generated from explicit parameters and seeds:

```
value_t = clip(round(base·(1 + weekly_t) + effect_t + sd·noise_t [+ shared factor]), 0, 100)
```

base 8–70 (log-uniform), noise 5–15 % (AR(1), φ 0–0.5), 40 % with a weekend uplift of
20–60 %, 20 % with 0–15 % missing days (left missing), half of the null-scale set sharing
one of 20 common factors (correlated queries). Events: spike (1–2 days), step, ramp
(10–27 days), limited-time event (2–4 day plateau, fast decay) and drop, magnitude
0.25–2.5× base.

**Labels are defined by the generator, independent of every detector threshold:** an
injected upward event is a *surge* if its peak effect ≥ max(10 index points, 50 % of
the base level); smaller upward effects are *ambiguous* (excluded from precision and
recall); drops are not surges. Each label records its onset and its "observable onset"
(first period at surge size).

## Metrics (`detection/evaluation.py`)

| Metric | Definition |
|---|---|
| alert | an episode's first CANDIDATE/CONFIRMED notification, or a reopening (escalation within an episode is not a new alert) |
| match | triggering observation period within [onset − 1, end + 3] of a label of the same series |
| precision | TP / (TP + FP); alerts matching only ambiguous labels are excluded |
| recall | detected surge labels / surge labels |
| detection delay | decision time − time the onset observation became available (days); also from the observable onset |
| false alerts per day | FP / evaluation days; also per 1,000 monitored series |
| duplication rate | additional alerts matching an already detected label, per detected label |
| coverage | share of series-periods in the window evaluated with a READY baseline |
| severity usefulness | Spearman correlation of episode maximum severity with the label's relative magnitude (TPs) |
| confirmed precision / recall | the same for episodes that reached CONFIRMED |

## Causal replay

Evaluation runs the production engine, alert policy and episode rules in memory
(`DetectionReplayer`). Values become available 36 h after their day ends; decisions are
made at daily steps and use only data available then. For data without revisions that
arrives in period order the fast path processes each series once and assigns every
evaluation to the step in which its observation appeared; this is identical to step-wise
replay (tested). Revised and late data are evaluated with step-wise replay on the Phase 3
replay engine. The decision log separates event time (observation window), availability,
decision time and later revisions; a revision never rewrites an earlier decision.

## Calibration and leakage control

* **Walk-forward**: thresholds and null distributions are fitted on the **training
  split** only (event-free training series); the **test split** (disjoint seeds, events
  and topic clusters) is evaluated once with the frozen configuration.
* Thresholds were never tuned on the test split. Tuning (CUSUM k/h from the ARL table,
  the GPD tail check) used training data only; the configuration was frozen
  (`thresholds-2026-11-13-synthetic`) before the test evaluation was run.
* Baselines are causal (no future values); revised values are not used before their
  availability; no topic cluster spans both splits; no event is reused across splits.
* A calibration fitted on synthetic data is applied to synthetic data only.

Run it yourself:

```bash
trend-agent detect calibrate --dataset synthetic         # training split -> stored nulls
trend-agent detect evaluate --dataset synthetic          # held-out test + null scale
```

## Results (held-out synthetic test split)

Run once with the frozen configuration (`detection-v1`, thresholds
`thresholds-2026-11-13-synthetic`) on 2026-11-13 via `trend-agent detect evaluate
--dataset synthetic`; raw output: [benchmarks/phase4_evaluation.json](benchmarks/phase4_evaluation.json).

* calibration: 155 event-free training series × 150 days → 20,741 null scores per
  stratum (anomaly score, CUSUM statistic); both GPD tail checks passed;
* test split: 400 series × 150 days, evaluation window = last 60 days (days 90–150),
  99 surge labels, 41 alerts matched only ambiguous (sub-threshold) events;
* null scale (scenario L): 1,000 series × 120 days without events, half sharing one of
  20 common factors, evaluation window = last 60 days.

| Set | Precision | Recall | False alerts / day | per 1,000 series / day | Median delay (p90) | Duplicates | CONFIRMED precision / recall |
|---|---|---|---|---|---|---|---|
| test, calibrated | **0.925** (99 TP / 8 FP) | **1.00** (99/99) | 0.13 | 0.33 | 0.5 d (6.5 d) | 0 | 0.973 / 0.737 (median 1.5 d) |
| test, no calibration | 0.884 (99 / 13) | 1.00 | 0.22 | 0.54 | 0.5 d (6.5 d) | 0 | – (cannot confirm) |
| null scale, calibrated | – | – | 1.10 | **1.10** (66 in 60 d) | – | – | 1 false CONFIRMED in 60,000 series-days |

* Recall by event type: spike 27/27, step 18/18, ramp 28/28, limited-time event 26/26.
* Detection delay is measured from the time the onset value became available (values
  appear 36 h after the day ends; decisions run daily), so 0.5 d means "at the first daily
  decision after publication". Ramps dominate the p90.
* Severity usefulness: Spearman ρ = 0.575 (p = 4.7·10⁻¹⁰) between episode maximum
  severity and the true relative magnitude.
* Coverage: 1.00 of series-periods in the window had a READY baseline (the benchmark
  provides 90 days of history before the window).
* False alerts by category (test, calibrated): AI 4, technology 3, gaming 1. Null scale:
  35 on independent and 31 on correlated series.
* The alert budget did not change the window results (66 false alerts with or without
  it). During the **warm-up** (first 60 days of history) the null series produced 264
  alerts without the budget (≈ 4.4 per 1,000 series per day), mostly weekly patterns
  before the seasonal model qualifies and provisional baselines; the budget held 182
  notification attempts back there. New series therefore alert more often than
  established ones.

Against the aspirational targets (precision ≥ 0.8, recall ≥ 0.7, ≤ 2 false alerts/day,
low delay, minimal duplicates, deterministic replay, no indirect evidence classified as
direct): met **on synthetic data** in steady state, not during the warm-up of new
series; unknown on real data.


## Limits of this evaluation

* Synthetic noise, seasonality and events are simple parametric shapes; real search
  interest has richer dynamics (holidays, news cycles, provider resampling).
* The calibration that makes `STATISTICALLY_SUPPORTED` possible is synthetic; real data
  needs its own calibration from stored in-control history.
* Detection delays include the simulated 36 h publication lag and daily decision steps.
* Labels count one event per series; overlapping real events are not modelled.
