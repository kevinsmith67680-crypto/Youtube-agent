# Opportunity windows (Phase 6)

An opportunity window answers one narrow question: **if a creator starts now and needs
a given production time, is the measured interest likely to still be elevated when the
video could be published?** It estimates demand persistence and timing only.

It does **not** predict views, ranking, click-through or the success of any video. It
does not promise that publishing inside the window will generate views, and it does not
suggest what to produce.

## Production profiles (explicit planning assumptions)

Configured in `config/forecasting.yaml` → `windows.profiles`. They are hypothetical
production times, not measurements:

| profile | production time | earliest publication |
|---|---|---|
| QUICK_REACTION | 2 h | issue time + 2 h |
| STANDARD_TUTORIAL | 8 h | issue time + 8 h |
| DEEP_DIVE | 24 h | issue time + 24 h |

Profiles can be added or changed in the configuration. Every window in the output
repeats its profile's hours and the resulting `earliest_publication` time, so the
assumption travels with the estimate.

## How the probability is estimated

The probability that interest is still elevated at publication is an **empirical share of
comparable historical states** (`peak_estimation.AnalogModel`):

1. The current state is described by 11 standardised features: level vs baseline,
   24/72 h change, recent slope, acceleration, growth persistence, window-over-window
   growth, distance to the breakout threshold, detector elevated streak and CUSUM ratio.
2. The 60 nearest training states (Euclidean distance on robustly standardised
   features) are taken. If fewer than 25 exist, or the nearest neighbours are farther
   than the 99th percentile of typical neighbour distances × 1.5, there is no estimate.
3. Among those analogs, the share that was still at or above *their own* breakout
   threshold at the nearest observable lead ≥ the production time (2, 6, 8, 12, 24, 48 or
   72 h) is the probability, with a Wilson 90 % interval.

Leads must be observable period ends: a daily series has no 2-hour lead, so the
QUICK_REACTION window of a daily series is UNKNOWN, never interpolated.

The analog model is an **experimental** model (`ANALOG_PEAK`). It is trained only on the
training segment and evaluated on the untouched test segment (peak timing error,
interval coverage, censoring rate).

## Classification

Thresholds are in `windows` (defaults shown):

| status | rule |
|---|---|
| UNKNOWN | not applicable (interest is neither elevated nor likely to break out: breakout probability < 0.3 and log((level+1)/(baseline+1)) ≤ 0.2, i.e. less than about 22 % above baseline), not estimable (too few comparable states), or the forecast itself was withheld |
| LIKELY_TOO_LATE | interest is elevated now (or the peak may have passed) and P(elevated at publication) < 0.25 |
| URGENT | P(elevated at publication) ≥ 0.6 and the estimated peak is within 24 h or may already be passing |
| LONGER_WINDOW | P(still elevated after 72 h) ≥ 0.6: persistent demand |
| TIMELY | P(elevated at publication) ≥ 0.6 otherwise |

A low probability for interest that is **not** elevated yet gives UNKNOWN, not
LIKELY_TOO_LATE: there is no established demand to be late for.

## Interpreting a window

* **"TIMELY, STANDARD_TUTORIAL, p = 0.72 [0.61, 0.81]"** means: among about 60
  comparable past states, about 72 % were still above their breakout threshold 8 h later.
  It is a base rate from similar situations, not a guarantee for this topic.
* The interval reflects only the number of comparable states. It does not cover model
  misspecification or a topic unlike anything in the training data (that case is
  `PEAK_NOT_ESTIMABLE` / UNKNOWN or OUT_OF_DISTRIBUTION).
* Windows are computed for the measured source. A proxy series (video activity) gives
  windows of **proxy attention**, labelled as such, never of search demand.
* Models trained on the synthetic corpus give windows only for synthetic series, and
  those windows say nothing about real topics.

## Stored records

Each window is stored in `opportunity_windows` (profile, production hours, estimated
publication time, status, probability, interval and evidence in
`uncertainty_metadata`), linked to its forecast. It expires with the forecast's source
observations. `trend-agent forecast windows [--status URGENT] [--include-synthetic]`
lists them.
