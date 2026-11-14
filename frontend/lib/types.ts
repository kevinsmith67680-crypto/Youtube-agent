// Types mirroring the dashboard API (`dashboard/1.0`, `recommendation/1.0`).
// Unavailable values are `null` with a reason elsewhere in the payload; the UI never
// substitutes a number for them.

export type DataClass = "OBSERVED" | "FORECAST" | "PROXY_SIGNAL" | "SYNTHETIC" | "MISSING";
export type RecStatus =
  | "APPROVED"
  | "APPROVED_WITH_UNCERTAINTY"
  | "RESEARCH_REQUIRED"
  | "REJECTED";
export type StatementLabel =
  | "VERIFIED_FACT"
  | "PLANNED_DEMONSTRATION"
  | "HYPOTHESIS_TO_TEST"
  | "UNKNOWN";
export type ProductionStatus =
  | "IDEA"
  | "RESEARCHING"
  | "SCRIPTING"
  | "RECORDING"
  | "EDITING"
  | "READY_TO_PUBLISH"
  | "PUBLISHED"
  | "ABANDONED";
export type FeedbackType =
  | "USEFUL"
  | "NOT_RELEVANT"
  | "TOO_COMPETITIVE"
  | "TOO_LATE"
  | "UNVERIFIED"
  | "SAVED"
  | "DISMISSED"
  | "PUBLISHED"
  | "VIEWED"
  | "SELECTED"
  | "PRODUCED"
  | "ABANDONED";

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface ApiErrorBody {
  error: string;
  detail: unknown;
  status: number;
}

// ------------------------------------------------------------------ opportunities
export interface OpportunitySummary {
  id: string;
  query: string;
  category: string | null;
  region: string | null;
  language: string | null;
  intent: string;
  sub_intent: string | null;
  demand_basis: string;
  demand_label: string;
  direct_youtube_search: boolean;
  surge_stage: string | null;
  surge_severity: number | null;
  gap_type: string | null;
  gap_strength: string | null;
  forecast_status: string | null;
  breakout_probability: number | null;
  breakout_calibration: string | null;
  opportunity_score: number | null;
  time_sensitive: boolean;
  urgency: string | null;
  evidence_quality: string | null;
  verification_state: string;
  status: string;
  recommendation_count: number;
  approved_count: number;
  last_updated: string;
  synthetic: boolean;
}

export interface GapView {
  gap_type: string;
  statement: string;
  evidence_strength: string;
  verification_status: string;
  requirement_keys: string[];
  limitations: string[];
  evidence_ids: string[];
}

export interface GapRow {
  opportunity_id: string;
  query: string;
  category: string | null;
  region: string | null;
  is_primary: boolean;
  gap: GapView;
  opportunity_score: number | null;
  opportunity_status: string;
  demand_label: string;
  recommendation_count: number;
  analysed_at: string;
  synthetic: boolean;
}

export interface RequirementView {
  key: string;
  requirement: string;
  priority: string;
  coverage: string;
  statement: string;
  covered_by: string[];
  evidence_tier: string;
}

export interface CompetingVideoView {
  video_id: string;
  url: string;
  published_at: string | null;
  relevance_score: number;
  entity_match: string;
  video_format: string;
  freshness: string;
  evidence_tier: string;
  finding_basis: string;
}

export interface OpportunityDetail {
  summary: OpportunitySummary;
  demand_statement: string;
  demand_limitations: string[];
  intent_entities: Record<string, unknown>[];
  requirements: RequirementView[];
  gaps: GapView[];
  primary_gap: GapView | null;
  competing_videos: CompetingVideoView[];
  supply_scope: string;
  explanation: Record<string, string>;
  main_uncertainty: string;
  coverage_limitations: string[];
  score_components: Record<string, unknown>[];
  claims: Record<string, unknown>[];
  recommendations: RecommendationSummary[];
  forecasts: ForecastView[];
  notice: string;
}

// ------------------------------------------------------------------------ trends
export interface SeriesPoint {
  period_start: string;
  period_end: string;
  available_at: string | null;
  value: number | null;
  quality: string;
  revision: number;
}

export interface TrendSeriesView {
  series_id: number | null;
  label: string;
  query: string;
  provider: string;
  measurement: string;
  measurement_definition: string;
  data_class: DataClass;
  granularity: string;
  unit: string;
  region: string | null;
  scale_type: string;
  synthetic: boolean;
  points: SeriesPoint[];
  freshness_hours: number | null;
  note: string;
}

export interface SurgeEpisodeView {
  id: number | null;
  query: string;
  start: string | null;
  end: string | null;
  detected_at: string | null;
  stage: string | null;
  classification: string | null;
  severity: number | null;
  measurement: string | null;
  statement: string;
  synthetic: boolean;
}

export interface ForecastPointView {
  lead_hours: number;
  forecast_time: string;
  value: number | null;
  lower: number | null;
  upper: number | null;
  interval_level: number | null;
  method: string;
  model: string;
}

export interface PeakView {
  status: string;
  horizon_hours: number;
  earliest_hours: number | null;
  latest_hours: number | null;
  median_hours: number | null;
  probability_beyond_horizon: number | null;
  analogs: number;
  experimental: boolean;
  note: string;
}

export interface WindowView {
  production_profile: string;
  production_hours: number;
  earliest_publication: string;
  probability_elevated_at_publication: number | null;
  probability_interval: [number, number] | null;
  horizon_covered: boolean;
  status: string;
  uncertainty: string;
  note: string;
}

export interface ForecastView {
  prediction_id: string;
  query: string;
  horizon_hours: number;
  status: string;
  status_reasons: string[];
  issued_at: string;
  data_cutoff: string;
  breakout_probability: number | null;
  breakout_calibration: string | null;
  continuation_probability: number | null;
  baseline_index: number | null;
  threshold_index: number | null;
  trajectory: ForecastPointView[];
  peak: PeakView | null;
  windows: WindowView[];
  target_family: string;
  source_classification: string;
  is_proxy: boolean;
  synthetic: boolean;
  model: string | null;
  notice: string;
}

export interface TrendSummary {
  id: string;
  query: string;
  category: string | null;
  provider: string;
  measurement: string;
  data_class: DataClass;
  granularity: string;
  latest_value: number | null;
  latest_at: string | null;
  freshness_hours: number | null;
  surge_stage: string | null;
  opportunity_id: string | null;
  synthetic: boolean;
}

export interface EvidenceRef {
  evidence_id: string;
  kind: string;
  trust: string | null;
  publisher: string | null;
  url: string | null;
  published_at: string | null;
  note: string;
}

export interface TrendDetail {
  summary: TrendSummary;
  series: TrendSeriesView[];
  episodes: SurgeEpisodeView[];
  forecasts: ForecastView[];
  evidence: EvidenceRef[];
  announcements: Record<string, unknown>[];
  limitations: string[];
}

// --------------------------------------------------------------- recommendations
export interface RecommendationSummary {
  id: string;
  opportunity_id: string;
  title: string;
  query: string;
  category: string | null;
  video_format: string;
  format_label: string;
  intent: string;
  sub_intent: string | null;
  score: number | null;
  rank: number | null;
  status: RecStatus;
  research: boolean;
  suppressed: boolean;
  suppression_reason: string | null;
  urgency: string;
  hours_low: number;
  hours_high: number;
  production_profile: string;
  complexity: string;
  demand_basis: string;
  forecast_available: boolean;
  synthetic: boolean;
  saved: boolean;
  dismissed: boolean;
  production_status: ProductionStatus | null;
  generated_at: string;
}

export interface TitleOption {
  strategy: string;
  text: string;
  rationale: string;
  target_intent: string;
  query_term_coverage: number;
  length: number;
  accepted: boolean;
  reasons: string[];
}

export interface Hook {
  hook_type: string;
  text: string;
  estimated_seconds: number;
  accepted: boolean;
  verification_status: string;
  reasons: string[];
}

export interface OutlineItem {
  label: StatementLabel;
  text: string;
  evidence_ids: string[];
  note: string | null;
}

export interface OutlineSection {
  heading: string;
  purpose: string;
  items: OutlineItem[];
}

export interface Outline {
  sections: OutlineSection[];
  required_evidence: string[];
  failure_points: string[];
  conclusion_to_investigate: string;
  removed_items: string[];
}

export interface FormatChoice {
  video_format: string;
  label: string;
  rationale: string;
  serves_intent: boolean;
  preferred_by_creator: boolean;
}

export interface WindowSummary {
  production_profile: string;
  status: string;
  earliest_publication: string | null;
  probability_elevated_at_publication: number | null;
}

export interface Feasibility {
  complexity: string;
  production_profile: string;
  profile_hours: number;
  estimated_hours_low: number;
  estimated_hours_high: number;
  breakdown_hours: Record<string, number>;
  required_software: string[];
  required_hardware: string[];
  requires_verified_results: boolean;
  creator_has_requirements: boolean | null;
  fits_creator_capacity: boolean | null;
  demand_window: WindowSummary | null;
  urgency: string;
  urgency_reasons: string[];
  uncertain: boolean;
  notes: string[];
}

export interface ScoreComponent {
  name: string;
  value: number | null;
  weight: number;
  contribution: number | null;
  available: boolean;
  raw: Record<string, number | string | boolean | null>;
  note: string;
}

export interface Ranking {
  composite_score: number | null;
  eligible: boolean;
  eligibility_reasons: string[];
  version: string;
  components: ScoreComponent[];
  available_weight: number;
  evidence_quality_factor: number;
  rank: number | null;
  explanation: string;
  limitations: string[];
  note: string;
}

export interface VerificationCheck {
  check: string;
  passed: boolean;
  detail: string;
}

export interface RecommendationVerification {
  status: RecStatus;
  synthetic: boolean;
  gate_state: string | null;
  gate_publication: string | null;
  gate_run_id: string | null;
  trend_statement: string | null;
  checks: VerificationCheck[];
  removed: string[];
  labels: string[];
  reasons: string[];
  verified_at: string;
}

export interface DemandSummary {
  basis: string;
  measurement_type: string | null;
  classification: string | null;
  alert_stage: string | null;
  severity: number | null;
  relative_change: number | null;
  freshness: string | null;
  detected_at: string | null;
  direct_youtube_search: boolean;
  indirect: boolean;
  statement: string;
  limitations: string[];
  evidence_ids: string[];
}

export interface ForecastSummary {
  available: boolean;
  prediction_id: string | null;
  status: string | null;
  status_reasons: string[];
  horizon_hours: number | null;
  is_proxy: boolean;
  breakout_probability: number | null;
  breakout_calibration: string | null;
  continuation_probability: number | null;
  trajectory_value: number | null;
  trajectory_lower: number | null;
  trajectory_upper: number | null;
  peak_status: string | null;
  peak_earliest_hours: number | null;
  peak_latest_hours: number | null;
  windows: WindowSummary[];
  model: string | null;
  validated_model: boolean | null;
  issued_at: string | null;
  synthetic: boolean;
  note: string;
}

export interface RecommendationOutput {
  schema_version: string;
  recommendation_id: string;
  opportunity_id: string;
  concept_index: number;
  category: string | null;
  region: string | null;
  language: string | null;
  primary_query: string;
  target_intent: string;
  target_sub_intent: string | null;
  title: string;
  alternative_titles: TitleOption[];
  hooks: Hook[];
  video_format: string;
  format_label: string;
  format_rationale: string;
  alternative_approaches: FormatChoice[];
  target_audience: string;
  core_viewer_problem: string;
  premise: string;
  why_it_may_satisfy_demand: string;
  key_information_requirements: string[];
  key_questions: string[];
  outline: Outline;
  evidence: EvidenceRef[];
  competing_videos: string[];
  uncertainties: string[];
  production: Feasibility;
  ranking: Ranking;
  verification: RecommendationVerification;
  demand: DemandSummary;
  forecast: ForecastSummary;
  gap_type: string | null;
  gap_strength: string | null;
  research_hypothesis: boolean;
  suppression: { suppressed: boolean; reason: string | null; duplicate_of: string | null };
  creator_profile_id: string | null;
  generator: string;
  generator_model: string | null;
  input_fingerprint: string;
  generated_at: string;
  versions: Record<string, string>;
  synthetic: boolean;
  notice: string;
}

export interface ProductionView {
  status: ProductionStatus;
  planned_publication_time: string | null;
  actual_publication_time: string | null;
  published_video_id: string | null;
  notes: string | null;
  updated_at: string;
  history: { status: string; at: string }[];
}

export interface UserState {
  saved: boolean;
  saved_at: string | null;
  notes: string | null;
  dismissed: boolean;
  production: ProductionView | null;
  feedback: Record<string, number>;
}

export interface RecommendationDetail {
  recommendation: RecommendationOutput;
  user: UserState;
  opportunity: OpportunitySummary | null;
}

export interface SavedIdea {
  recommendation: RecommendationSummary;
  saved_at: string;
  notes: string | null;
  production: ProductionView | null;
}

// ------------------------------------------------------------------------ system
export interface ProviderView {
  name: string;
  kind: string;
  status: string;
  configured: boolean;
  detail: string;
  last_success: string | null;
}

export interface FreshnessView {
  provider: string;
  measurement: string;
  series: number;
  latest_observation: string | null;
  age_hours: number | null;
  status: string;
}

export interface OverviewView {
  mode: string;
  synthetic: boolean;
  monitored_queries: number | null;
  active_surges: number | null;
  opportunities: number | null;
  approved_recommendations: number | null;
  research_candidates: number | null;
  recent_topics: {
    query: string;
    category: string | null;
    analysed_at: string;
    status: string;
    opportunity_id: string;
  }[];
  providers: ProviderView[];
  freshness: FreshnessView[];
  category_distribution: Record<string, number>;
  top_recommendations: RecommendationSummary[];
  notes: string[];
}

export interface SystemStatusView {
  mode: string;
  synthetic: boolean;
  version: string;
  database: string;
  components: Record<string, string>;
  llm: Record<string, unknown>;
  versions: Record<string, string>;
  counts: Record<string, number | null>;
  notifications: ProviderView[];
  generated_at: string;
}

export interface DataQualityView {
  items: FreshnessView[];
  notes: string[];
}

export interface AlertView {
  id: string;
  event_type: string;
  severity: string;
  title: string;
  body: string;
  created_at: string;
  read: boolean;
  recommendation_id: string | null;
  opportunity_id: string | null;
  synthetic: boolean;
  deliveries: { channel: string; status: string; attempts?: number; last_error?: string | null }[];
}

export interface CreatorProfile {
  profile_id: string;
  display_name: string;
  preferred_categories: string[];
  channel_focus: string;
  primary_regions: string[];
  language: string;
  preferred_formats: string[];
  production_capacity_videos_per_week: number | null;
  available_hours_per_week: number | null;
  skills: string[];
  equipment: string[];
  preferred_duration_minutes: number | null;
  channel_size: string | null;
  content_exclusions: string[];
  topic_preferences: string[];
  production_style: string;
  recommendation_quantity: number;
  notification_preferences: Record<string, boolean>;
  llm_daily_token_budget: number | null;
}

export interface Health {
  status: string;
  mode: string;
  synthetic: boolean;
  version: string;
  auth: "configured" | "read-only";
}
