# Forecast targets (Phase 6)

Every training label is computed from the series' **own observed values**. No label uses
the Phase 4 detector's alert status, a surge-episode stage or any model output; a
detector alert is never treated as proof that a breakout happened. The definitions are
versioned (`TARGET_VERSION = targets-1.1.0`, `src/trend_agent/forecasting/target_definition.py`)
and stored with every model (`target_definition`), so a model is never compared with
labels it was not trained on.

All values are **relative interest indices of the measured source** (YouTube Search,
external web search, or a proxy such as video activity). No target is a search count, a
view count or a forecast of any individual video's performance.

## Reference state at the prediction time T

Only observations *available* at T (publication time, else collection/ingestion time:
`availability_mode`) are used. Revisions published after T are invisible at T.

| quantity | definition |
|---|---|
| baseline B | median of the visible values in the trailing baseline window: 168 h for hourly series, 672 h (28 days) for daily series |
| spread s | 1.4826 × MAD of the same values (a robust standard deviation) |
| threshold θ | B + max(0.5·B, 10 index points, 2·s); the 10-point floor applies only to bounded 0–100 indices (consistent or per-request relative scales) |
| current level | median of the last p visible values (p = 3 hourly, 1 daily) |
| elevated | current level ≥ θ |

At least 48 (hourly) or 10 (daily) baseline observations are required, otherwise there is
no reference and no label (`insufficient_baseline`). A level older than max(24 h, 3
periods) gives `stale_level`.

## Outcome window and maturity

The outcome for horizon H uses the **final** values (latest revisions) of the periods
ending in (T, T + H]. A label exists only if

1. the horizon is a whole number of periods for the series' granularity (a daily series
   has no 6-hour target; nothing is interpolated);
2. at least `min_label_coverage` = 75 % of those periods were observed; and
3. the window has **matured**: the data reaches T + H. A window that runs past the end of
   the data is never labelled, because a "no breakout" there might be a breakout that has
   not happened yet. Datasets built "as of" a date see only data available then, so
   immature labels stay unknown.

## A. Breakout (classification)

Applicable only when the series is **not** elevated at T.

`breakout_H = 1` if at least p consecutive future periods in (T, T + H] reach θ
(p = 3 for hourly, 1 for daily), else 0. A single spike shorter than p periods is not a
breakout.

## C. Continuation (classification)

Applicable only when the series **is** elevated at T.

`continuation_H = 1` if the median of the last quarter of the window (T, T + H] is still
≥ θ, else 0.

## B. Trajectory (regression)

`trajectory_H = log((y(T + H) + 1) / (level + 1))`, the log change of the series' own
index from the current level to the value of the period ending exactly at T + H. It is
scale-free and baseline-free. Converting it back gives an index value of the same
source, never an absolute volume.

## D. Peak timing (within a window, with censoring)

Within the peak window (T, T + 72 h]:

* `peak_offset` = hours from T to the highest final value;
* `peak_censored = 1` when that maximum is at the last observed period of the window, or
  the last value is ≥ 0.97 × the maximum: interest may still be rising and the peak may
  lie **beyond** the window (right-censoring);
* `peak_passed = 1` when no future value exceeds the current level.

The peak window must cover the longest horizon. Only matured windows with ≥ 75 % coverage
are labelled. A peak offset describes a local maximum inside the window, not the
lifetime peak of a topic.

## E. Persistence at publication leads (opportunity windows)

`elevated_lead_d` for d ∈ {2, 6, 8, 12, 24, 48, 72} h = 1 if the final value of the period
ending exactly at T + d is ≥ θ. A lead exists only where it is an observable period end
for the granularity: a daily series has no 2-hour lead, so the quick-reaction window of a
daily series is UNKNOWN rather than interpolated.

## Statuses that replace a missing peak

| status | meaning |
|---|---|
| ESTIMATED | enough comparable historical states peaked inside the window; a 10–90 % range and median offset are given |
| PEAK_LIKELY_BEYOND_HORIZON | most comparable states were still rising at the window's end (censored); no time is given |
| PEAK_MAY_HAVE_PASSED | comparable states mostly never exceeded their current level |
| PEAK_NOT_ESTIMABLE | too few or too dissimilar comparable states |

A precise peak timestamp is never reported.

## Known caveats

* **Baseline absorption on long plateaus.** The baseline is a trailing median. If
  interest stays high for longer than about half the baseline window (3.5 days hourly,
  14 days daily), the baseline rises towards the plateau and θ rises with it. Later
  prediction times then see "not elevated" and later breakout labels need a new rise.
  This is deliberate (a long plateau is the new normal), but it means "breakout" always
  means *relative to the recent past*.
* **Relative indices.** For per-request relative scales (e.g. a provider rescaling 0–100
  per request) only scale-free features and labels are used. Absolute index differences
  are disabled (`scale-dependent` features are NaN).
* **Revisions.** Labels use the latest revision of each period. A model may therefore be
  scored against values that were not yet known at prediction time. This is the correct
  outcome definition (what really happened), but it is one more reason why historical
  accuracy is approximate.
