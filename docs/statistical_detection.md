# Statistical detection

All detectors are **causal**: the result for period *t* uses only values of periods
before *t* (and the value of *t* itself) that were available at the evaluation time.
Every quantity is a deterministic function of that data and the configuration, which is
what makes incremental processing identical to recomputation (tested bit for bit) and
replay reproducible. Parameters live in `config/detection.yaml`; their version and digest
are stored with every result.

## 1. Preprocessing (`detection/preprocessing.py`)

1. **Knowledge at T** – per observation window, the highest revision available at the
   evaluation time (provider publication time, else the conservative
   `max(collected_at, ingested_at)`). Conflict rows never count.
2. **Comparability** – all values must share one series signature (provider, effective
   measurement, region, language, granularity, unit, scale type, normalisation key).
   Mixed input → `INCOMPATIBLE` with the Phase 3 reason codes plus
   `normalization_context_changed`. Nothing is rescaled or stitched; independently
   normalised Trends-style batches are never compared.
3. **Grid** – hour / UTC day / Monday week slots. Empty slots are *missing periods*, rows
   without a value are *missing values*; both stay `NaN`. **No interpolation, no zero
   fill.**
4. **Quality** – partial periods are excluded; low-volume values are kept and flagged;
   values outside the scale bounds → `INVALID_SCALE`; presence and rank scales →
   `UNSUPPORTED_SCALE` (they are not magnitudes).
5. **Status** – `READY`, `PARTIALLY_READY` (gaps, short history), `INSUFFICIENT_DATA`
   (< 7 usable values: cold start), `IRREGULAR_SAMPLING` (< 50 % of the last 56 daily
   slots usable: sequential detectors are disabled), plus the failure statuses above.

0–100 values are **relative indices**. A move from 2 to 8 can be statistically surprising
and still not a meaningful surge, so every alerting path also requires a minimum absolute
effect (8 index points / 3 items) and a minimum observed level (10 / 3).

## 2. Baselines (`detection/baseline.py`)

### Robust rolling baseline

For period *t*, over the usable values in the 28 periods before *t* (168 hours, 26 weeks):

```
median_t = median(x_i)                      (x_t excluded)
MAD_t    = median(|x_i - median_t|)
sigma_t  = 1.4826 * MAD_t
z_t      = (x_t - median_t) / (sigma_t + epsilon)
```

* `READY` with ≥ 14 values, `PROVISIONAL` with 7–13 (stricter thresholds; never
  CONFIRMED), otherwise no baseline (cold start).
* **Zero MAD** (over half the window equals the median – flat or heavily rounded data):
  the mean absolute deviation × 1.2533 is used; if that is also ~0 or below the floor, a
  floor is used: max(1 index point, 5 % of the level), and √expected for counts (Poisson).
  MAD-zero and floor dispersions are flagged **unstable**: they cannot support
  `STATISTICALLY_SUPPORTED` or `CONFIRMED`, and reduce severity.
* Uncertainty: the median's standard error ≈ 1.2533 σ/√n gives a 95 % interval for the
  expected value and a predictive interval; `z_range_95` shows how the score moves if
  the expected value sits at either end of its interval.

### Seasonal profile baseline

An additive weekday profile for daily data or hour-of-day profile for hourly data – the
cycle follows the data resolution, so **no hourly pattern is ever fitted to daily data**.
The profile is refitted once per calendar block (ISO week / UTC day) from the 12 cycles
before the block:

1. trend = centred moving median over one full cycle (seasonality-free);
2. profile s_p = median of (x − trend) for phase p, centred;
3. **Kruskal–Wallis** test of a phase effect on the detrended values;
4. one-step-ahead errors of both predictors inside the fit window: seasonal
   (level = median of the deseasonalised values of the previous 2 cycles, plus s_p) and
   robust rolling.

The seasonal model is selected only with ≥ 6 cycles of history, ≥ 5 values per phase,
Kruskal–Wallis p < 0.01 **and** ≥ 10 % lower one-step error dispersion than the robust
model. Otherwise the robust baseline is used and the reason is recorded. Expected value =
level + s_p; dispersion = robust dispersion of the seasonal one-step errors (honest
prediction error, not in-sample residuals).

Every estimate records: model and version, sample count, expected value, dispersion and
its source, readiness, resolution, seasonal component and the selection reason.

## 3. Rapid anomaly detector (`detection/anomaly.py`)

Score = max(z, 0) against the baseline; a drop never produces an upward alert (it is
labelled `downward_change`). Thresholds come from `anomaly.defaults` and ordered
overrides by provider, granularity, category, readiness and scale family:

| stratum | watch z | alert z |
|---|---|---|
| ready baseline (default) | 3.0 | 4.0 |
| hourly data | 3.5 | 5.0 |
| count signals | 3.5 | 5.0 |
| provisional baseline (wins) | 4.0 | 6.0 |

A threshold crossing that fails the absolute effect or level floor is `REJECTED` with
reasons. Relative growth is reported only when the expected value is ≥ 5 (index) / 2
(count). A **p-value exists only when a calibration exists** for the stratum (§7).

## 4. CUSUM sustained-change detector (`detection/cusum.py`)

```
G_t = max(0, G_{t-1} + min(z_t, z_clip) - k)        alarm when G_t > h
```

* Same causal standardised residuals as the anomaly detector, READY baselines only.
* Winsorised at `z_clip = 3`: one extreme value adds at most `z_clip − k = 2 < h`, so an
  alarm needs several elevated periods – a sustained shift, not a spike (enforced by
  configuration validation).
* Missing periods add no evidence; a gap > 3 periods resets G (`gap_reset`). Irregular
  series do not run CUSUM.
* After an alarm G resets and no new alarm is raised for 7 periods (refractory).
* State (G, run start, last slot, refractory, alarm counts) is persisted per series.
  Out-of-order and revised data cause a deterministic recomputation (§6); the state is
  never patched in place.

**Calibration of k and h.** k = δ/2 for a minimum meaningful drift δ = 2 robust sd
(k = 1.0). h was chosen from the empirical in-control ARL table on synthetic null
residuals (training split, `detect calibrate`): with autocorrelated, heavy-tailed,
rounded data the empirical ARL0 is orders of magnitude below the normal-theory
(Siegmund) value, e.g. k = 0.5, h = 5 → 87 periods empirically vs 938 in theory. The
target ARL0 ≥ 1,000 periods comes from the false-alert budget (≤ ~1 CUSUM false alarm
per day across 1,000 daily series); k = 1.0, h = 6 gives ≈ 1,380 on the training nulls.
These are **synthetic-derived** values and must be recalibrated on real history.

## 5. Change-point detector: BOCPD (`detection/change_points.py`)

**Choice.** Bayesian Online Change Point Detection (Adams & MacKay 2007) – one
well-understood method, chosen because it complements the other two detectors instead
of repeating them: it does not use the rolling baseline, it locates the change, and it
estimates the levels before and after it. Page–Hinkley was rejected because it is
essentially another CUSUM.

**Model.** Segments of i.i.d. Normal values with unknown mean and variance
(Normal–Gamma prior: μ0 = first value, κ0 = 1, α0 = 1, β0 = α0·s0²); Student-t posterior
predictive; constant hazard 1/λ, λ = 100. The run-length posterior is truncated at 200
(tail mass merged). Seasonal series feed deseasonalised values. Missing values are
skipped (run length counts observations).

**Decision.** Upward change when the posterior mass on "the current segment started
3–10 observations ago" ≥ 0.6, and the segment median exceeds the pre-change median
(28 values before it) by ≥ 1 robust sd and the minimum absolute effect. Watch at ≥ 0.35.
A lone spike cannot satisfy "≥ 3 observations after the change".

**Complexity.** O(R) per observation (R ≤ 200), state ≈ 5 × 201 float64 values plus a
ring buffer, serialised exactly (base64 float64).

**Assumptions / failure modes.** Piecewise stationarity; roughly symmetric noise. Gradual
ramps appear as several small or delayed changes (CUSUM covers them); strong
autocorrelation inflates change probabilities; bounded indices near 0 or 100 violate
normality; the prior scale floor matters for very flat series.

## 6. Engine, state and out-of-order data (`detection/engine.py`)

The engine processes the periods of one series in order and returns one evaluation per
newly available observation. State is persisted per series and detector type
(`detector_states`: engine watermark, seasonal-fit cache, CUSUM, BOCPD, cold start).

* **Incremental = recomputed**: processing only new periods with the saved state gives
  the same numbers as recomputing all loaded history (unit-tested, including a state
  serialisation round trip).
* A new observation for an **earlier period** (late arrival) or a **revised value** →
  recomputation from the start of the loaded history (`recompute_reason`). Results
  already recorded for earlier revisions stay as detection-time records; the new
  revision gets its own result; late values older than 3 periods are recorded but never
  open alerts.
* A changed algorithm version or configuration digest → recomputation.

## 7. Multiple testing (`detection/significance.py`, `detection/calibration.py`)

**Valid p-values only from calibration.** A stratum (provider, granularity, measurement,
scale family, detector) gets an empirical null distribution of its statistic from a
**training split** of in-control data. p = (1 + #null ≥ s)/(n + 1) inside the observed
tail; beyond the largest null value, an empirical p-value cannot go below 1/(n + 1),
which would make FDR control over hundreds of hypotheses impossible, so the tail above
the 95th percentile is fitted with a **generalised Pareto distribution** (peaks over
threshold). The fit is used only if it predicts the observed exceedance counts at the
99/99.5/99.9 % in-sample quantiles within a factor 2 (rounded data have ties, so a KS
test would reject any continuous fit) and has shape < 0.5; extrapolation is limited to
two orders of magnitude below 1/(n + 1). The basis (`empirical`, `gpd_extrapolated`,
`conservative_bound`, `below_tail`) is reported. Calibrations fitted on synthetic data
apply to synthetic series only.

**Per-run FDR.** All calibrated hypotheses of one detection run (anomaly score and CUSUM
statistic per new observation) form one family; Benjamini–Hochberg at q = 0.05 (valid
under independence and positive regression dependence, which correlated queries in one
topic typically show) or Benjamini–Yekutieli (any dependence). An anomaly-only trigger
whose p-value is not accepted stays WATCH. **Repeating BH every run is not an online
FDR guarantee over time** – that is stated in every alert.

**Alert budget.** Independently, new notifications are capped at 25 per rolling 24 h,
and the target of ≤ 2 false alerts per day across the monitored scope drives the CUSUM
ARL and the threshold choice. Without a calibration this is the only error-rate control
and results are `EMPIRICALLY_THRESHOLDED`. Each run records hypotheses, candidates,
accepted alerts, the budget, the threshold version and the evaluation time.

## 8. Cold start (`detection/cold_start.py`)

Series without a baseline are not forced through the anomaly detector. States:
`NEW_TOPIC`, `EARLY_INTEREST_OBSERVED`, `INSUFFICIENT_BASELINE`,
`COLD_START_GROWTH_CANDIDATE` (≥ 3 values, last ≥ 1.5× first and the maximum, ≥ minimum
effect, and either independent corroboration – official announcement / code release /
suggestions / creator videos / community discussion from ≥ 2 providers in 14 days – or
strong monotone growth, Kendall τ ≥ 0.6 over ≥ 4 values), `BASELINE_ESTABLISHED`.
Confidence is always `LOW_CONFIDENCE_COLD_START`; the highest stage is CANDIDATE with
corroboration and WATCH without; never CONFIRMED. Corroboration explains why interest
might rise; it is never treated as a measurement.

## 9. Failure modes and limits

| Situation | Behaviour |
|---|---|
| strong weekly pattern, < 6 weeks history | robust baseline flags weekends (counted in calibration; seasonal model takes over later) |
| flat or heavily rounded series | unstable dispersion: no statistical support, reduced severity |
| ramps slower than the rolling window adapts | CUSUM catches them; very slow drifts (< 2 sd over the window) are not targeted |
| per-request scaled sources | each batch is its own series; only within-batch detection |
| provider changes normalisation | new series, cold start again; cross-context comparison refused |
| heavy autocorrelation | handled empirically by calibration (ARL table, null tails), not by a model |
| no calibration (all real data here) | p-values absent; nothing can be CONFIRMED |
