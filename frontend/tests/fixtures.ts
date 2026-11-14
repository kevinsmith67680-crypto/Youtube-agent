import type {
  ForecastView,
  OpportunitySummary,
  RecommendationSummary,
  TrendSeriesView,
  UserState,
} from "@/lib/types";

export function rec(over: Partial<RecommendationSummary> = {}): RecommendationSummary {
  return {
    id: "7c913baa-4dbb-511b-b40a-6ea9399422e4",
    opportunity_id: "6cac6ef5-640d-5d4c-afc3-ebcca92f0a41",
    title: "How to Install CodeForge 2: Complete Setup Guide",
    query: "how to install codeforge 2",
    category: "ai",
    video_format: "LOCAL_AI_INSTALLATION_GUIDE",
    format_label: "Local AI installation guide",
    intent: "tutorial",
    sub_intent: "model_installation",
    score: 65.4,
    rank: 1,
    status: "APPROVED",
    research: false,
    suppressed: false,
    suppression_reason: null,
    urgency: "UNKNOWN",
    hours_low: 5.9,
    hours_high: 11.8,
    production_profile: "STANDARD_TUTORIAL",
    complexity: "MEDIUM",
    demand_basis: "direct_search_candidate",
    forecast_available: true,
    synthetic: true,
    saved: false,
    dismissed: false,
    production_status: null,
    generated_at: "2026-11-13T12:00:00Z",
    ...over,
  };
}

export function opp(over: Partial<OpportunitySummary> = {}): OpportunitySummary {
  return {
    id: "6cac6ef5-640d-5d4c-afc3-ebcca92f0a41",
    query: "how to install codeforge 2",
    category: "ai",
    region: "US",
    language: "en",
    intent: "tutorial",
    sub_intent: "model_installation",
    demand_basis: "direct_search_candidate",
    demand_label: "YouTube Search surge candidate",
    direct_youtube_search: true,
    surge_stage: "candidate",
    surge_severity: 0.64,
    gap_type: "missing_specific_answer",
    gap_strength: "weak",
    forecast_status: "READY",
    breakout_probability: 0.26,
    breakout_calibration: "CALIBRATED",
    opportunity_score: 72,
    time_sensitive: false,
    urgency: null,
    evidence_quality: "moderate",
    verification_state: "VERIFIED",
    status: "candidate",
    recommendation_count: 5,
    approved_count: 5,
    last_updated: "2026-11-13T12:00:00Z",
    synthetic: true,
    ...over,
  };
}

export function forecast(over: Partial<ForecastView> = {}): ForecastView {
  return {
    prediction_id: "11111111-2222-3333-4444-555555555555",
    query: "how to install codeforge 2",
    horizon_hours: 24,
    status: "READY",
    status_reasons: [],
    issued_at: "2026-11-13T12:00:00Z",
    data_cutoff: "2026-11-13T12:00:00Z",
    breakout_probability: 0.26,
    breakout_calibration: "CALIBRATED",
    continuation_probability: null,
    baseline_index: 18,
    threshold_index: 28,
    trajectory: [
      {
        lead_hours: 24,
        forecast_time: "2026-11-14T12:00:00Z",
        value: 35,
        lower: 22,
        upper: 48,
        interval_level: 0.8,
        method: "conformal",
        model: "lightgbm",
      },
    ],
    peak: null,
    windows: [],
    target_family: "search_interest",
    source_classification: "direct_youtube_search_interest",
    is_proxy: false,
    synthetic: true,
    model: "lightgbm_breakout",
    notice: "Forecasts describe relative interest in the measured source only.",
    ...over,
  };
}

export function series(over: Partial<TrendSeriesView> = {}): TrendSeriesView {
  return {
    series_id: 1,
    label: "direct youtube search interest (synthetic)",
    query: "how to install codeforge 2",
    provider: "synthetic_demo_trends",
    measurement: "direct_youtube_search_interest",
    measurement_definition: "Relative YouTube Search interest index (not a number of searches)",
    data_class: "SYNTHETIC",
    granularity: "hour",
    unit: "index",
    region: "US",
    scale_type: "consistent_index",
    synthetic: true,
    points: [
      { period_start: "2026-11-13T10:00:00Z", period_end: "2026-11-13T11:00:00Z", available_at: null, value: 20, quality: "ok", revision: 1 },
      { period_start: "2026-11-13T11:00:00Z", period_end: "2026-11-13T12:00:00Z", available_at: null, value: 31, quality: "ok", revision: 1 },
    ],
    freshness_hours: 0.2,
    note: "Synthetic test data.",
    ...over,
  };
}

export function user(over: Partial<UserState> = {}): UserState {
  return { saved: false, saved_at: null, notes: null, dismissed: false, production: null, feedback: {}, ...over };
}
