# Forecast accuracy (Phase 6)

> **Everything on this page is measured on SYNTHETIC, fictional data.** No real held-out
> YouTube Search (or external search) history is available in this environment. These
> numbers show that the pipeline trains, calibrates, abstains and selects models the way
> it is designed to. They are **not** evidence of real-world forecasting accuracy, and no
> model here is a production-validated real-world model.

Reproduce with `python scripts/evaluate_forecasting.py --scale 1.0 --workers 4` (about
2.5 minutes on 4 CPUs). Full output: [`docs/benchmarks/phase6_evaluation.json`](benchmarks/phase6_evaluation.json).

## How accuracy is measured

* **Independent labels.** Outcomes are computed from the series' own final values
  ([forecast_targets.md](forecast_targets.md)), never from the detector's alerts.
* **Chronological testing.** Per scope: train → embargo (horizon + 48 h) → calibration →
  embargo → untouched test segment. 20 % of topic groups never appear in training or
  calibration (unseen topics in the test segment).
* **No test-period tuning.** Hyperparameters, decision thresholds, calibrators and the
  selected model are all chosen without the test segment. It is used once, as a gate and
  for these numbers.
* **Baselines on the same rows.** Every model is compared with climatology (the training
  base rate) and, for LightGBM, with logistic regression on the same test rows.
  Comparisons use a paired **series-block bootstrap** (400 resamples, 90 % interval).
* **Metrics.** Brier score, Brier skill (1 − Brier/Brier_ref), PR-AUC, ROC-AUC (never
  alone), ECE (10 bins), precision@50, false-positive rate at the calibrated threshold,
  lead time (how many hours before the end of a breakout's run-up the first alert came),
  forecast stability; trajectory MAE of the log change, MAE scaled by persistence,
  interval coverage; peak timing error and interval coverage on identifiable peaks.
* **Held-out corpus.** Champions are re-scored on a second synthetic corpus with a
  different seed (series the models never saw), on its chronological test segment.

Corpus: 360 synthetic series (240 hourly × 56 days, 120 daily × 240 days; AI 45 %,
technology 25 %, gaming 30 %), 72,098 examples, dataset `ds-37bbaa8ce3c0`, leakage audit
0/6 mismatches. Held-out corpus: 180 series, 35,969 examples, leakage audit 0/4.

## Breakout probability (untouched test segment)

Brier scores (lower is better). Δ = LightGBM − logistic Brier with its 90 % bootstrap
interval.

| scope | test rows / positives | climatology | logistic: Brier · PR-AUC · ECE · P@50 | LightGBM: Brier · PR-AUC · ECE · P@50 | Δ LightGBM − logistic | selected |
|---|---|---|---|---|---|---|
| hourly 6 h | 9,310 / 58 | 0.0062 | 0.0054 · 0.241 · 0.001 · 0.34 | 0.0054 · 0.278 · 0.002 · 0.44 | −0.0000 [−0.0005, 0.0004] | **logistic** (LightGBM not significantly better) |
| hourly 24 h | 8,689 / 211 | 0.0237 | 0.0216 · 0.195 · 0.007 · 0.46 | 0.0197 · 0.296 · 0.003 · 0.50 | −0.0019 [−0.0029, −0.0010] | **LightGBM** |
| hourly 72 h | 6,811 / 450 | 0.0618 | 0.0569 · 0.234 · 0.012 · 0.40 | 0.0543 · 0.278 · 0.007 · 0.66 | −0.0025 [−0.0048, −0.0001] | **LightGBM** |
| daily 24 h | 4,919 / 94 | 0.0187 | 0.0164 · 0.240 · 0.008 · 0.40 | 0.0159 · 0.254 · 0.006 · 0.42 | −0.0005 [−0.0014, 0.0004] | **logistic** (LightGBM not significantly better) |
| daily 72 h | 4,564 / 151 | 0.0320 | 0.0290 · 0.241 · 0.013 · 0.56 | 0.0261 · 0.330 · 0.008 · 0.60 | −0.0029 [−0.0054, −0.0004] | **LightGBM** |

Every selected model beats climatology with a bootstrap interval excluding zero (e.g.
hourly 24 h LightGBM: −0.0040 [−0.0069, −0.0015]). False-positive rates at the
calibrated threshold are 0.3–3 %. Daily series have no 6-hour scope.

LightGBM is deliberately *not* selected where it is not significantly better than
logistic regression (hourly 6 h, daily 24 h), even though its point estimates look
slightly better there.

**Rolling origins** (logistic refit per expanding fold, PR-AUC per fold) show stability
over time, e.g. hourly 24 h 0.293 / 0.301 / 0.256 and daily 72 h 0.222 / 0.327 / 0.287.

### Slices (selected model, test segment)

Brier skill against each slice's own observed base rate:

| scope | AI | technology | gaming |
|---|---|---|---|
| hourly 6 h | 0.16 (PR-AUC 0.32) | 0.02 (0.13) | 0.14 (0.29) |
| hourly 24 h | 0.18 (0.35) | 0.30 (0.45) | **−0.02 (0.05)** |
| hourly 72 h | 0.16 (0.38) | 0.11 (0.30) | **0.00 (0.09)** |
| daily 24 h | 0.16 (0.33) | 0.24 (0.51) | **−0.01 (0.11)** |
| daily 72 h | 0.22 (0.41) | 0.29 (0.50) | 0.04 (0.14) |

In this corpus, **gaming breakouts are essentially unpredictable**: they are dominated by
abrupt limited-time events and spikes with no run-up. The models know it, which is the
desired behaviour: their gaming probabilities stay near the base rate (gaming-slice ECE 0.003–0.014)
rather than alerting falsely. Slice sizes are in the JSON. The `history` slice contains
only established series, because the test segment lies at the end of the timeline;
cold-start behaviour is covered by eligibility (INSUFFICIENT_HISTORY) and the unseen-topic
groups instead.

### Continuation (already elevated)

| scope | result |
|---|---|
| hourly 6 h | logistic validated: 203 test rows (144 positives), Brier 0.097 vs 0.207 climatology, ECE 0.037, but a false-positive rate of 0.31: continuation is the majority outcome |
| hourly 24 h | none validated: 189 test rows < 200 required (baseline-only) |
| hourly 72 h | none validated: does not beat climatology, ECE 0.12 > 0.08, only 149 test rows |
| daily 24 / 72 h | not trained: 215 / 201 training rows < 300 required |

Elevated states are rare, so continuation models mostly abstain. That is the intended
outcome for small samples.

## Held-out corpus (different seed, never seen in training)

| champion | test rows / positives | Brier | Brier of the corpus's own base rate | skill | PR-AUC | ECE | episodes flagged · mean lead |
|---|---|---|---|---|---|---|---|
| hourly 6 h breakout (logistic) | 4,585 / 44 | 0.0084 | 0.0095 | 0.12 | 0.295 | 0.005 | 39 % · 0.9 h |
| hourly 24 h breakout (LightGBM) | 4,293 / 137 | 0.0261 | 0.0309 | 0.16 | 0.307 | 0.010 | 61 % · 18.9 h |
| hourly 72 h breakout (LightGBM) | 3,356 / 308 | 0.0733 | 0.0834 | 0.12 | 0.325 | 0.026 | 65 % · 30.7 h |
| daily 24 h breakout (logistic) | 2,388 / 38 | 0.0167 | 0.0157 | **−0.07** | 0.108 | 0.011 | 36 % · 14.4 h |
| daily 72 h breakout (LightGBM) | 2,172 / 55 | 0.0240 | 0.0247 | 0.03 | 0.123 | 0.008 | 29 % · 30.0 h |
| hourly 6 h continuation (logistic) | 163 / 128 | 0.121 | 0.169 | 0.28 | 0.938 | 0.074 | — (FPR 0.60) |

The reference here is the held-out corpus's *own* observed base rate, a stricter
reference than the training climatology the acceptance checks use.

**The daily 24 h champion does not generalise to the held-out corpus**: it is slightly
worse than that corpus's base rate, although it passed every acceptance check on its own
test segment (38 positives is a small sample). Passing a single held-out period is
therefore not treated as proof. In operation, drift monitoring on matured labels would
report `worse_than_climatology` and recommend `revert_to_baseline`. With real data, a
champion should be confirmed on more than one held-out period before it is relied on.

## Trajectory (log change of the index, 80 % intervals)

MAE scaled by persistence (< 1 is better than "no change"):

| scope | reference baseline (chosen on calibration MAE) | LightGBM MAE (scaled) | LightGBM interval coverage (nominal 0.80) |
|---|---|---|---|
| hourly 6 h | seasonal naive 0.123 (0.64) | 0.083 (0.43) | 0.82 |
| hourly 24 h | persistence 0.124 (1.00) | 0.097 (0.78) | 0.81 |
| hourly 72 h | persistence 0.151 (1.00) | 0.138 (0.92) | 0.79 |
| daily 24 h | persistence 0.102 (1.00) | 0.084 (0.82) | 0.81 |
| daily 72 h | seasonal naive 0.112 (0.98) | 0.093 (0.81) | 0.80 |

LightGBM beats the reference baseline with a bootstrap interval excluding zero in every
scope. The damped-trend baseline is worst everywhere (scaled MAE 1.15–3.26): extrapolating
recent growth is a poor forecast for spiky interest. Observed interval coverage is close
to nominal on this synthetic data. Real series with stronger dependence or drift may
cover less ([forecast_uncertainty.md](forecast_uncertainty.md)).

## Peak timing (experimental)

| granularity | test examples | identified peaks | right-censored | not estimable | median abs. error | 10–90 % interval coverage |
|---|---|---|---|---|---|---|
| hourly (72 h window) | 6,720 | 6,200 | 7.7 % | 0.4 % | 20 h | 0.79 |
| daily (72 h = 3 periods) | 4,717 | 2,246 | 52 % | 60 % | 0 h* | 0.99* |

\* Daily peak timing is on a 24-hour lattice with three positions. The 0 h median error
and 99 % coverage reflect the coarse lattice and wide intervals, not precise timing.
Daily series are mostly censored or not estimable, which is the honest answer for so few
periods.

A 20-hour median timing error inside a 72-hour window is modest. Peak estimates are
reported as ranges with explicit censoring, never as timestamps, and always as
`experimental`.

## Interpretation and limits

* Absolute values (e.g. Brier 0.02) depend on base rates set by the synthetic generator;
  only the comparisons against baselines on the same rows are meaningful.
* The synthetic corpus encodes assumptions (event mix, adoption lengths, noise). A model
  that learns those assumptions can look good here and fail on real data. The held-out
  daily 24 h result already shows how fragile small-sample validation is.
* Real-world validation requires real monitored history with matured labels: train with
  `--dataset database`, which refuses synthetic rows, then confirm the champions on later
  held-out periods and with `forecast drift` before relying on them.
