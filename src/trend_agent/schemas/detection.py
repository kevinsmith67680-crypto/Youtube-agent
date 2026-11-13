"""Phase 4 detection vocabulary: detectors, baselines, readiness, support and run types.

Statistical labels here describe *how much the data supports a statement*, never more:

* ``STATISTICALLY_SUPPORTED`` is used only when the score was converted into a p-value by
  a calibration fitted on data of the same kind (provider, granularity, measurement) and
  the multiple-testing procedure accepted it;
* ``EMPIRICALLY_THRESHOLDED`` means a configured threshold was exceeded, with no
  calibrated error rate behind it;
* ``LOW_CONFIDENCE_COLD_START`` means there is no reliable baseline at all.
"""

from __future__ import annotations

from enum import StrEnum


class DetectorType(StrEnum):
    ROBUST_ANOMALY = "robust_anomaly"  # sharp one-period upward deviation from baseline
    CUSUM = "cusum"  # one-sided CUSUM on standardised residuals (sustained drift)
    CHANGE_POINT = "change_point"  # Bayesian online change point (level shift)
    COLD_START = "cold_start"  # sparse / new series (no reliable baseline)
    ENGINE = "engine"  # per-series bookkeeping (watermark, baseline model, counters)


ALERTING_DETECTORS = (DetectorType.ROBUST_ANOMALY, DetectorType.CUSUM, DetectorType.CHANGE_POINT)


class BaselineModel(StrEnum):
    ROBUST_ROLLING = "robust_rolling"  # rolling median / MAD of prior observations
    SEASONAL_PROFILE = "seasonal_profile"  # robust level + weekday/hour-of-day profile
    NONE = "none"  # not enough comparable history


class BaselineReadiness(StrEnum):
    READY = "ready"  # enough comparable prior observations for the selected model
    PROVISIONAL = "provisional"  # usable for WATCH/CANDIDATE only (short history)
    INSUFFICIENT = "insufficient"  # cold start: no statistical baseline


class DispersionSource(StrEnum):
    MAD = "mad"  # 1.4826 * median absolute deviation
    MEAN_ABS_DEV = "mean_abs_dev"  # MAD was zero: 1.2533 * mean absolute deviation
    POISSON = "poisson"  # count series: sqrt(expected count) floor
    FLOOR = "floor"  # every robust estimate was ~0: configured floor (unstable)


class PreprocessStatus(StrEnum):
    READY = "ready"
    PARTIALLY_READY = "partially_ready"  # usable with caveats (gaps, short history)
    INSUFFICIENT_DATA = "insufficient_data"
    INCOMPATIBLE = "incompatible"  # mixed scales / normalisation contexts / categories
    IRREGULAR_SAMPLING = "irregular_sampling"
    INVALID_SCALE = "invalid_scale"  # values outside the declared scale bounds
    UNSUPPORTED_GRANULARITY = "unsupported_granularity"
    UNSUPPORTED_SCALE = "unsupported_scale"  # presence / rank: not a magnitude
    NO_DATA = "no_data"


class ResultStatus(StrEnum):
    TRIGGERED = "triggered"  # detector threshold exceeded
    WATCH = "watch"  # elevated, below the alert threshold
    NOT_TRIGGERED = "not_triggered"
    REJECTED = "rejected"  # exceeded but failed a validation rule (reason recorded)
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_APPLICABLE = "not_applicable"  # e.g. CUSUM on a cold-start series


class SupportStatus(StrEnum):
    STATISTICALLY_SUPPORTED = "statistically_supported"
    EMPIRICALLY_THRESHOLDED = "empirically_thresholded"
    LOW_CONFIDENCE_COLD_START = "low_confidence_cold_start"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_SUPPORTED = "not_supported"


class ColdStartState(StrEnum):
    NEW_TOPIC = "new_topic"  # no usable observation yet
    EARLY_INTEREST_OBSERVED = "early_interest_observed"  # first non-zero measurements
    INSUFFICIENT_BASELINE = "insufficient_baseline"  # some data, no growth pattern
    COLD_START_GROWTH_CANDIDATE = "cold_start_growth_candidate"  # repeated upward movement
    BASELINE_ESTABLISHED = "baseline_established"  # conventional detection applies


class RunType(StrEnum):
    LIVE = "live"  # `detect run`
    WORKER = "worker"  # event-driven queue consumer
    HOOK = "hook"  # in-process after Phase 3 collection
    REPLAY = "replay"
    EVALUATION = "evaluation"
    CALIBRATION = "calibration"
    DEMO = "demo"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # some series failed (errors recorded)
    FAILED = "failed"


class MultipleTestingMethod(StrEnum):
    BH = "benjamini_hochberg"  # FDR under independence / positive dependence (PRDS)
    BY = "benjamini_yekutieli"  # FDR under arbitrary dependence (more conservative)
    ALERT_BUDGET = "alert_budget"  # no valid p-values: thresholds + daily alert budget


class Reason(StrEnum):
    """Machine-readable reasons attached to detector results and alert decisions."""

    BELOW_MIN_EFFECT = "below_min_absolute_effect"
    BELOW_MIN_LEVEL = "below_min_observed_level"
    UNSTABLE_DENOMINATOR = "unstable_dispersion_estimate"
    PARTIAL_PERIOD = "partial_period"
    LOW_VOLUME = "low_volume_value"
    MISSING_VALUE = "missing_value"
    INCOMPARABLE = "incomparable_history"
    NORMALIZATION_CHANGED = "normalization_context_changed"
    CONFLICTING_VALUE = "conflicting_value"
    DOWNWARD_CHANGE = "downward_change"
    REVISED_AWAY = "supporting_value_revised_away"
    LATE_REVISION = "late_revision_not_alertable"
    BUDGET_EXHAUSTED = "alert_budget_exhausted"
    NOT_ACCEPTED_BY_FDR = "not_accepted_by_fdr_control"
    UNCALIBRATED = "thresholds_not_calibrated_for_this_data"
    COLD_START = "cold_start_no_reliable_baseline"
    PROVISIONAL_BASELINE = "provisional_baseline"
    INSUFFICIENT_HISTORY = "insufficient_history"
    IRREGULAR_CADENCE = "irregular_cadence"
    GAP_RESET = "gap_reset"
    REFRACTORY = "refractory_after_alarm"
    PROXY_EVIDENCE = "indirect_attention_signal_not_search_demand"
    EXTERNAL_EVIDENCE = "external_search_property_not_youtube"
    SUGGESTION_PRESENCE = "suggestion_presence_not_search_volume"
    NOT_PERSISTENT = "not_yet_persistent"
    STALE_SOURCE = "source_not_current"
    SYNTHETIC = "synthetic_test_data"
    INVALID_SCALE = "value_outside_scale_bounds"
