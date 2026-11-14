import type { Tone } from "@/lib/labels";

// Twitch gaming intelligence (livestream activity on another platform). Mirrors /api/twitch/*.

export type TwitchEvidenceClass =
  | "TWITCH_ACTIVITY"
  | "OBSERVED_VIEWERS"
  | "PARTIAL_SAMPLE"
  | "RANK_CHANGE"
  | "PROXY_OPPORTUNITY"
  | "DIRECT_YOUTUBE_SEARCH_INTEREST";

export const TWITCH_EVIDENCE: Record<TwitchEvidenceClass, { label: string; tone: Tone; description: string }> = {
  TWITCH_ACTIVITY: {
    label: "Twitch activity",
    tone: "info",
    description:
      "Top-game rankings and live streams from the Twitch Helix API: external gaming attention on another platform. Not YouTube search interest.",
  },
  OBSERVED_VIEWERS: {
    label: "Observed viewers",
    tone: "neutral",
    description:
      "Concurrent viewers of the live streams the API actually returned, summed. Not a total for the game unless the sample is complete, and never a number of searches.",
  },
  PARTIAL_SAMPLE: {
    label: "Partial sample",
    tone: "warning",
    description:
      "The collection stopped before the API's list ended (page limit or error): viewer and stream counts cover only part of the live streams.",
  },
  RANK_CHANGE: {
    label: "Rank change",
    tone: "info",
    description:
      "Change in a game's position in Get Top Games between two stored observations, over their actual interval. Twitch returns no viewer totals for rankings.",
  },
  PROXY_OPPORTUNITY: {
    label: "Proxy opportunity",
    tone: "warning",
    description:
      "A possible YouTube video opportunity inferred from Twitch attention and a verified game entity. A hypothesis to investigate, not demand.",
  },
  DIRECT_YOUTUBE_SEARCH_INTEREST: {
    label: "Direct YouTube Search interest",
    tone: "critical",
    description: "Only from a live-verified direct YouTube Search provider. Twitch data never produces this class.",
  },
};

export const TWITCH_EVIDENCE_ORDER: TwitchEvidenceClass[] = [
  "TWITCH_ACTIVITY",
  "OBSERVED_VIEWERS",
  "PARTIAL_SAMPLE",
  "RANK_CHANGE",
  "PROXY_OPPORTUNITY",
  "DIRECT_YOUTUBE_SEARCH_INTEREST",
];

export const NOT_SEARCH_DEMAND =
  "Twitch data is livestream activity on another platform (external gaming attention). It is not YouTube search volume or search interest and cannot confirm a YouTube Search surge.";

export const STATE_LABEL: Record<string, string> = {
  rising: "Rising",
  sustained_rise: "Sustained rise",
  steady: "Steady",
  falling: "Falling",
  insufficient_observations: "Not enough observations",
  incomparable_samples: "Samples not comparable",
};

export const STATE_OPTIONS = [
  { value: "", label: "Any attention status" },
  { value: "rising", label: "Rising" },
  { value: "sustained_rise", label: "Sustained rise" },
  { value: "steady", label: "Steady" },
  { value: "falling", label: "Falling" },
  { value: "insufficient_observations", label: "Not enough observations" },
];

export interface TwitchGame {
  game_id: string;
  name: string;
  igdb_id: string | null;
  category_kind: string;
  latest_rank: number | null;
  best_rank: number | null;
  rank_observed_at: string | null;
  positions_gained: number | null;
  window_positions_gained: number | null;
  new_in_top: boolean;
  observed_viewers: number | null;
  observed_streams: number | null;
  sample_status: string | null;
  viewers_observed_at: string | null;
  viewers_pct_per_hour: number | null;
  viewer_comparison: string | null;
  attention_state: string;
  attention_score: number;
  attention_version: string | null;
  labels: string[];
  entity_id: number | null;
  promoted_at: string | null;
  last_seen_at: string | null;
  freshness_minutes: number;
  expires_at: string | null;
  in_latest_ranking?: boolean;
  rank_change_since_previous_poll?: number | null;
}

export interface TwitchGames {
  items: TwitchGame[];
  total: number;
  snapshot: { observed_at: string | null; previous_observed_at: string | null; games: number; sample_status: string | null } | null;
  demo: boolean;
  detail?: string;
  caveat?: string;
}

export interface TwitchStream {
  stream_id: string;
  user_login: string;
  user_name: string | null;
  game_id: string | null;
  game_name: string | null;
  title: string | null;
  language: string | null;
  started_at: string | null;
  viewer_count: number;
  position: number;
  observed_at: string | null;
  channel_url: string;
}

export interface TwitchStreams {
  items: TwitchStream[];
  snapshot: {
    kind: string;
    observed_at: string | null;
    sample_status: string;
    status_reason: string;
    pages_fetched: number;
    pages_requested: number;
    streams: number | null;
    viewer_sum: number | null;
    languages: string[];
  } | null;
  shown_viewer_sum?: number;
  demo: boolean;
  detail?: string;
}

export interface Freshness {
  snapshots: number;
  last_observed_at: string | null;
  age_minutes: number | null;
}

export interface TwitchSummary {
  demo: boolean;
  detail?: string;
  games: number;
  games_real?: number;
  rising_games?: number;
  monitored_games?: number;
  rank_observations?: number;
  viewer_observations?: number;
  stream_observations?: number;
  freshness: Record<string, Freshness>;
  retention?: { hours: number; extended_storage_authorized: boolean; policy: string };
  provider: { status: string; detail?: string; api_enabled?: boolean; scheduled_discovery?: boolean; credentials?: string };
  caveat?: string;
}

export interface AttentionComponent {
  value: number;
  weight: number;
  contribution: number;
  basis: string;
}

export interface KeywordHypothesis {
  keyword_id: number;
  query: string;
  template: string | null;
  intent: string;
  status: string;
  priority: number;
}

export interface FormatCandidate {
  format: string;
  eligible: boolean;
  grounded_by: string[];
  requires: string;
  queries: string[];
}

export interface PhaseLinks {
  content_gaps: { gap_type: string; evidence_strength: string; explanation: string }[];
  content_opportunities: { query: string; score: number | null; status: string; demand_basis: string }[];
  recommendations: { id: string; title: string; query: string; video_format: string; status: string; demand_basis: string }[];
  forecasts: { query_id: number; target_family: string; source_classification: string; forecast_status: string; horizon_hours: number }[];
}

export interface TwitchOpportunity extends TwitchGame, PhaseLinks {
  entity: { id: number; name: string; status: string; evergreen: boolean } | null;
  keyword_hypotheses: KeywordHypothesis[];
  format_candidates: FormatCandidate[];
}

export interface TwitchGameDetail extends TwitchGame, PhaseLinks {
  attention: {
    version?: string;
    state?: string;
    score?: number;
    components?: Record<string, AttentionComponent>;
    notes?: string[];
    caveats?: string[];
    viewers?: { comparison?: string | null; interval_hours?: number | null; pct_change?: number | null; viewer_delta?: number | null; latest_sample_status?: string | null };
    rank?: { positions_gained?: number | null; interval_hours?: number | null; status?: string };
  };
  history: {
    window_hours: number;
    ranks: { observed_at: string; rank: number }[];
    top_games_polls: number;
    polls_outside_top_n: number;
    viewers: {
      observed_at: string;
      viewers_observed: number;
      streams_observed: number;
      sample_status: string;
      sample_basis: string;
      sample_cap: number;
      languages: string[];
    }[];
    note: string;
  };
  monitoring: { tier: string; origin: string; next_poll_at: string | null; last_polled_at: string | null } | null;
  entity: { id: number; name: string; type: string; status: string; evergreen: boolean } | null;
  possible_explanations: { title: string; url: string | null; publisher: string | null; published_at: string | null; relationship: string }[];
  keyword_hypotheses: KeywordHypothesis[];
  format_candidates: FormatCandidate[];
  direct_youtube_search_interest: { available: boolean; detail: string };
  caveat: string;
}
