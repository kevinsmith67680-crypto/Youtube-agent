# Surge alerts

A surge alert says that **what a source measures** rose abnormally or persistently. It
never states a number of searches (0–100 values are relative indices), and severity is a
ranking index, not a probability.

## Evidence classification (`schemas/evidence.py`)

| Series measures (Phase 3 category) | Classification | Highest stage |
|---|---|---|
| `DIRECT_YOUTUBE_SEARCH_INTEREST` | `DIRECT_SEARCH_SURGE` | CONFIRMED |
| `EXTERNAL_SEARCH_INTEREST` (e.g. Google Web) | `EXTERNAL_SEARCH_SURGE` | CANDIDATE |
| `VIDEO_ACTIVITY` | `PROXY_ATTENTION_SIGNAL` | CANDIDATE |
| `SEARCH_SUGGESTION_PRESENCE` | `PROXY_ATTENTION_SIGNAL` (presence ≠ volume) | CANDIDATE |
| `OFFICIAL_EVENT_SIGNAL` | `PROXY_ATTENTION_SIGNAL` | CANDIDATE |
| `COMMUNITY_ACTIVITY` | `PROXY_ATTENTION_SIGNAL` | CANDIDATE |
| several indirect categories rising for one target | `POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY` (target view) | CANDIDATE |

Rules enforced in code, in the output contract and (for CONFIRMED) by a database check
constraint: only direct YouTube search interest can be CONFIRMED; a proxy-flagged series
is never search interest; video attention never becomes a confirmed search-demand
surge; missing values are never zero; synthetic observations are classified by the
category they simulate but always carry `synthetic = true`, an `[SYNTHETIC]` explanation
prefix, and are excluded from production listings (`detect alerts`, Phase 5 feed) unless
explicitly requested.

## Stages (`detection/alert_policy.py`)

| Stage | Meaning |
|---|---|
| WATCH | preliminary unusual activity; more evidence needed |
| CANDIDATE | statistical evidence of a possible upward change |
| CONFIRMED | validated **direct** YouTube search-interest surge |
| INSUFFICIENT_EVIDENCE | the data cannot support a conclusion (recorded on results) |
| REJECTED | the apparent anomaly failed validation (effect too small, revised away …) |
| RESOLVED | a previously detected surge is no longer active |

CONFIRMED requires all of: direct evidence; READY baseline with stable dispersion; a
complete (not low-volume) value; elevation for ≥ 2 consecutive periods; comparable
history; and statistical support – a calibrated hypothesis of this observation accepted
by BH/BY in its run (`confirm_requires_calibration`). Detectors read the same data, so
agreement between them is never counted as independent evidence; it only raises the
severity's agreement bonus. Every decision records machine-readable reasons
(`schemas/detection.Reason`), e.g. `not_yet_persistent`,
`thresholds_not_calibrated_for_this_data`, `external_search_property_not_youtube`.

Confidence: `STATISTICALLY_SUPPORTED` (calibrated, FDR-accepted, ready, stable),
`EMPIRICALLY_THRESHOLDED` (threshold exceeded, no calibrated error rate),
`LOW_CONFIDENCE_COLD_START`, `INSUFFICIENT_DATA`, `NOT_SUPPORTED`.

## Severity (`detection/severity.py`)

```
statistical = 0.75 * max(deviation, persistence, change_point) + 0.25 * mean(...)
severity    = 100 * multiplier * (0.5 * statistical + 0.3 * magnitude + 0.2 * relative)
```

| Component | Normalised as |
|---|---|
| deviation | z / 8 (capped at 1) |
| persistence | max(elevated periods / 5, CUSUM statistic / h) |
| change_point | posterior probability of a recent *upward* change |
| magnitude | (observed − expected) / 30 index points (20 items for counts) |
| relative | log(observed/expected) / log 4, only when expected ≥ 5 (else its weight moves to magnitude) |
| multiplier | sample quality (low volume 0.7) × baseline readiness (provisional 0.8, cold 0.6) × freshness × unstable dispersion (0.7) |

Weights are versioned (`severity-v1`). Every component's raw value, normalised value,
weight and contribution is stored with the alert. For an open episode, severity is
measured against the episode's **frozen pre-episode baseline**, so an ongoing surge keeps
its severity while the rolling baseline adapts. Demand severity is kept separate from
content-opportunity ranking (a later phase).

## Episodes and deduplication (`detection/events.py`, `deduplication.py`)

An episode is one continuing event for (target, provider, evidence category, region,
language, synthetic flag). Per-request scaled series of one target share the key, so a
surge visible in several normalisation batches is one episode; keywords in the same
topic cluster keep separate episodes (`TargetSurgeView` groups them for presentation
without adding evidence).

| Rule | Default |
|---|---|
| a notification is recorded only when an episode first reaches CANDIDATE or CONFIRMED (creation, escalation, reopening) | – |
| escalation | immediate |
| downgrade | one stage after 2 periods of lower-stage evidence, but held while the value stays ≥ 3 sd above the pre-episode baseline |
| resolution | back within 1 sd of the pre-episode baseline for 2 periods; or no detector support for 14 periods (new normal); or no observation for 7 periods (stale) |
| rejection | the only supporting value was revised and no longer triggers |
| reopening | a CANDIDATE+ trigger within 7 days of resolution reopens the same episode |
| cooldown | WATCH-level triggers within 2 periods of resolution are ignored |
| budget | ≤ 25 new notifications per rolling 24 h; held-back ones stay WATCH with `alert_budget_exhausted` |

Idempotency: each episode remembers the observation revisions applied to it; processing
the same observation again (repeated runs, lost detector state) changes nothing. Late
history never drives the lifecycle. In PostgreSQL a transaction-scoped advisory lock per
episode key serialises concurrent workers, and a partial unique index allows only one
open episode per key. Transitions (`surge_event_transitions`) and evidence links
(`surge_evidence` → observations and detector results) are stored.

## Output contract (`schemas/surge.py`, `detect schema`)

`SurgeAlert` (`schema_version = "surge-alert/1.0"`) is stored as the episode payload and
re-validated on read. It carries: IDs (event, target, query, keyword, topic cluster);
category, region, language, provider; `measurement_type`, `is_proxy`, `classification`;
`alert_status`, `confidence_status`, cold-start state; severity, maximum severity and
components; episode start, first-detected, detected and updated times; observation window
and the observation reference (id, revision, series, value, **availability time and
basis**, collection and publication times, source reference, data policy); peak;
baseline summary (model + version, expected, observed, dispersion and source, sample
count, readiness, resolution, scale, unit, normalisation key, seasonal component,
absolute and relative difference); detector summaries (name, version, triggered, status,
score, threshold, calibrated p-value); multiple-testing summary (method, family size,
accepted, q, threshold and calibration version); comparability status and reasons;
source quality; freshness; limitations; deterministic explanation; reasons; evidence
provenance; algorithm and configuration versions; synthetic flag.

The model validates the evidence rules again: classification must match the measurement,
CONFIRMED needs direct evidence, a READY baseline, comparable history and no cold start,
`STATISTICALLY_SUPPORTED` needs an accepted calibrated hypothesis, and synthetic alerts
must be labelled. Phase 5 reads `DetectionService.feed()` (typed alerts) and
`DetectionService.target_views()` (combined per-target classification).

## Explanations (`detection/explanations.py`)

Deterministic templates filled from stored results, e.g.:

> YouTube search-interest index increased from an expected baseline of 20.0 to 57.0 for a
> comparable observation window (2026-10-02 00:00..2026-10-03 00:00 UTC). The robust
> anomaly detector triggered. The baseline is a robust rolling median of 28 prior
> observations (dispersion 6.7, ready). The source identifies the signal as YouTube Search
> interest. This is a relative index, not an absolute number of searches. The increase is
> statistically supported: a calibrated p-value was accepted by benjamini-hochberg control
> at q=0.05 among 38 hypotheses of this run (per-run false-discovery control, not an
> online guarantee).

> This topic has insufficient historical observations for a statistically reliable
> baseline (5 comparable observation(s); 14 required). Current evidence supports monitoring
> but not confirmation of a search surge.

No language model is involved; editorial text can be generated later from these
structured results.
