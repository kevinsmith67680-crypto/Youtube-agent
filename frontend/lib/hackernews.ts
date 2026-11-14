import type { Tone } from "@/lib/labels";

// Hacker News stories (community discussion activity). Mirrors /api/hackernews/* responses.

export type HNEvidenceClass =
  | "COMMUNITY_ACTIVITY"
  | "KEYWORD_HYPOTHESIS"
  | "EXTERNAL_ATTENTION"
  | "DIRECT_YOUTUBE_SEARCH_INTEREST";

export const HN_EVIDENCE_CLASS: Record<HNEvidenceClass, { label: string; tone: Tone; description: string }> = {
  COMMUNITY_ACTIVITY: {
    label: "Community activity",
    tone: "info",
    description:
      "Hacker News stories, points and comments, and their measured changes between stored observations. Community discussion activity: not views, not search interest, not search volume.",
  },
  KEYWORD_HYPOTHESIS: {
    label: "Keyword hypothesis",
    tone: "warning",
    description: "A search phrase generated from a verified entity. A hypothesis to test, never an observed search query.",
  },
  EXTERNAL_ATTENTION: {
    label: "External attention",
    tone: "neutral",
    description:
      "Independent sources outside Hacker News (official posts, GitHub, YouTube video metadata) mentioning the same entity. Several stories linking one announcement count once.",
  },
  DIRECT_YOUTUBE_SEARCH_INTEREST: {
    label: "Direct YouTube Search interest",
    tone: "critical",
    description:
      "Only from a live-verified direct YouTube Search provider. Hacker News data never produces this class.",
  },
};

export const HN_EVIDENCE_ORDER: HNEvidenceClass[] = [
  "COMMUNITY_ACTIVITY",
  "KEYWORD_HYPOTHESIS",
  "EXTERNAL_ATTENTION",
  "DIRECT_YOUTUBE_SEARCH_INTEREST",
];

export const LIST_OPTIONS = [
  { value: "", label: "All lists" },
  { value: "new", label: "New stories" },
  { value: "top", label: "Top stories" },
  { value: "best", label: "Best stories" },
  { value: "show", label: "Show HN" },
  { value: "ask", label: "Ask HN" },
];

export const KIND_LABEL: Record<string, string> = {
  story: "Story",
  show_hn: "Show HN",
  ask_hn: "Ask HN",
  launch_hn: "Launch HN",
  tell_hn: "Tell HN",
};

export const NOT_SEARCH_DEMAND =
  "Hacker News attention is community discussion activity. It is not YouTube search volume or search interest and cannot confirm a YouTube Search surge.";

export interface HNStory {
  story_id: number;
  title: string;
  linked_url: string | null;
  discussion_url: string;
  domain: string | null;
  author: string | null;
  kind: string;
  subject: string;
  primary_category: string;
  secondary_category: string | null;
  ambiguous: boolean;
  relevance: number;
  project_name: string | null;
  potential_intents: string[];
  flags: string[];
  lists: string[];
  published_at: string | null;
  age_hours: number;
  score: number;
  comments: number;
  last_observed_at: string | null;
  priority_score: number;
  momentum_state: string;
  score_per_hour: number | null;
  comments_per_hour: number | null;
  momentum_status: string | null;
  monitoring_state: string;
  refresh_tier: string;
  next_refresh_at: string | null;
  entity_id: number | null;
  first_seen_at: string | null;
  freshness_hours: number;
  source_key: string;
  evidence_class: HNEvidenceClass;
  signal_label: string;
  synthetic: boolean;
}

export interface HNList {
  items: HNStory[];
  total: number;
  page?: number;
  page_size?: number;
  demo: boolean;
  synthetic: boolean;
  detail?: string;
  caveat?: string;
}

export interface HNTrending {
  items: HNStory[];
  measured: number;
  awaiting_repeat_observation: number;
  window_hours: number;
  demo: boolean;
  detail?: string;
  caveat?: string;
}

export interface HNSourceHealth {
  source_key: string;
  list: string;
  enabled: boolean;
  poll_interval_minutes: number;
  health: string;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_new_items_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
}

export interface HNSummary {
  demo: boolean;
  synthetic: boolean;
  detail?: string;
  stories: number;
  active_stories?: number;
  rising_stories?: number;
  observations: number;
  refresh_observations?: number;
  entities_linked?: number;
  by_category?: Record<string, number>;
  by_list?: Record<string, number>;
  last_observed_at?: string | null;
  last_refresh_at?: string | null;
  freshness_hours?: number | null;
  sources: HNSourceHealth[];
  provider: { status: string; detail?: string; api_enabled?: boolean; scheduled_discovery?: boolean };
  caveat?: string;
}

export interface HNMomentum {
  signal_class?: string;
  signal_label?: string;
  status?: string;
  state?: string;
  observations?: number;
  interval_hours?: number | null;
  score_delta?: number | null;
  comments_delta?: number | null;
  score_per_hour?: number | null;
  comments_per_hour?: number | null;
  notes?: string[];
  caveats?: string[];
}

export interface PriorityComponent {
  value: number;
  weight: number;
  contribution: number;
  basis: string;
}

export interface HNKeywordCandidate {
  keyword_id: number;
  query: string;
  origin: string;
  method: string;
  template: string | null;
  intent: string;
  status: string;
  priority: number;
  evidence_class: HNEvidenceClass;
}

export interface HNObservation {
  observed_at: string;
  score: number | null;
  comments: number | null;
  origin: string;
  list: string | null;
  rank: number | null;
  source_key: string | null;
}

export interface HNStoryDetail extends HNStory {
  classification: Record<string, unknown>;
  priority_components: Record<string, PriorityComponent | Record<string, unknown>>;
  momentum: HNMomentum;
  observations: HNObservation[];
  entity: { id: number; name: string; type: string; status: string } | null;
  reposts: { story_id: number; discussion_url: string }[];
  keyword_candidates: HNKeywordCandidate[];
  external_attention: {
    evidence_class: HNEvidenceClass;
    independent_publishers: number;
    providers: string[];
    hn_stories_about_entity: number;
    basis: string;
  };
  direct_youtube_search_interest: { evidence_class: HNEvidenceClass; available: boolean; detail: string };
  caveat: string;
}

/** Priority components only (keys starting with "_" carry metadata, not a component). */
export function priorityComponents(d: HNStoryDetail["priority_components"]): [string, PriorityComponent][] {
  return Object.entries(d ?? {})
    .filter(([k, v]) => !k.startsWith("_") && typeof (v as PriorityComponent).contribution === "number")
    .map(([k, v]) => [k, v as PriorityComponent]);
}
