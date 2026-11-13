# Forecast uncertainty, calibration and abstention (Phase 6)

A forecast says how uncertain it is, and says nothing when it cannot be trusted.
Uncertainty is reported separately from the strength of a current surge: a strong surge
can still have a very uncertain future.

## Probability calibration

Breakout and continuation scores become probabilities only through a calibrator fitted
on the **calibration segment**, which is chronologically after the training data and
separated from it and from the test segment by an embargo
(`src/trend_agent/forecasting/calibration.py`).

| condition (calibration segment) | method | status |
|---|---|---|
| < 25 positives or < 25 negatives | none | `INSUFFICIENT_CALIBRATION_DATA`: no probability is reported |
| ≥ 600 examples and ≥ 60 positives | isotonic regression | `CALIBRATED` |
| otherwise | Platt (sigmoid) scaling | `CALIBRATED` |

* The final test segment is never used to fit a calibrator or choose a decision
  threshold. The F1-optimal threshold is chosen on the calibration segment.
* Expected calibration error (10 bins) is reported before and after calibration, with
  the reliability table and the support of each bin.
* A probability interval (Wilson, 90 %) is reported from the observed rate and count of
  the calibration bin the probability falls in. With few examples in a bin the interval
  is wide, as it should be.
* Models are promoted only if calibrated and with held-out ECE ≤ 0.08.

Statuses in the output (`breakout.calibration_status`):

| status | meaning | what the forecast contains |
|---|---|---|
| CALIBRATED | calibrated model, row in distribution | `probability` with interval |
| UNCALIBRATED | model without a calibrator | only an uncalibrated `score`, labelled as such; the forecast is LIMITED_CONFIDENCE |
| INSUFFICIENT_CALIBRATION_DATA | too few calibration labels | no probability |
| OUT_OF_DISTRIBUTION | the row is outside the training distribution | probability withheld |
| NOT_APPLICABLE | breakout does not apply (already elevated: continuation is forecast instead) | — |

## Prediction intervals (trajectory)

Trajectory intervals are **split-conformal** intervals: the 80 % quantile (finite-sample
corrected) of absolute residuals on the calibration segment, added around the point
forecast (`uncertainty.ConformalInterval`). Fewer than 30 calibration residuals gives no
interval.

Conformal coverage guarantees need exchangeable data. Time series are serially dependent
and can drift, so **the nominal level is not a guarantee**. Observed coverage on the
untouched test segment is reported next to the nominal level, and a trajectory model is
validated only if observed coverage is within ±0.10 of nominal. When no validated
trajectory model exists, the persistence baseline is used with an empirical interval from
the series' own recent history of changes at that horizon.

Intervals are on the log-change scale and are converted back to index values of the
same source. They are not intervals on search counts.

## Out-of-distribution detection

For every feature, the training range [0.5 %, 99.5 %] widened by half an inter-quartile
range is stored with the model (`uncertainty.OODReference`). For a new row:

* share of observed features outside their range > 5 % gives `LIMITED_CONFIDENCE`;
* share ≥ 20 %, or a category or granularity never seen in training, gives
  `OUT_OF_DISTRIBUTION`, and the probability is withheld;
* missing features are missing, not out of range.

## Abstention (forecast statuses)

| status | when |
|---|---|
| READY | eligible, champion models available, row in distribution |
| LIMITED_CONFIDENCE | some features near/outside the training range, or only an uncalibrated score |
| BASELINE_ONLY | no validated model for this scope: persistence trajectory with an empirical interval, no breakout probability |
| INSUFFICIENT_HISTORY | less than 168 h (hourly) / 672 h (daily) of history, too many missing recent periods (> 30 %), or no reference baseline |
| UNSUPPORTED_GRANULARITY | the horizon is not a whole number of the series' periods (e.g. 6 h on daily data), or the granularity is unsupported |
| SOURCE_UNAVAILABLE | not a forecast target (e.g. an official event signal), or the latest observation is older than 24 h (hourly) / 96 h (daily) |
| OUT_OF_DISTRIBUTION | see above |
| FORECAST_UNAVAILABLE | the normalization context changed inside the baseline window, or a champion artifact failed checksum verification (the model is refused, never silently replaced) |

Abstention is per horizon: a daily series can be READY at 24 h and 72 h and
UNSUPPORTED_GRANULARITY at 6 h.

## Synthetic and real models never mix

Models trained on the synthetic corpus have `data_origin = synthetic` and are selected
only for synthetic series. A real series without a real validated model gets a
BASELINE_ONLY forecast. A synthetic model is never presented as production-validated.
