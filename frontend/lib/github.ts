import type { Tone } from "@/lib/labels";

// GitHub discoveries (external developer activity). Mirrors /api/github/* responses.

export type EvidenceClass =
  | "GITHUB_ACTIVITY"
  | "VERIFIED_RELEASE"
  | "KEYWORD_HYPOTHESIS"
  | "EXTERNAL_ATTENTION"
  | "DIRECT_YOUTUBE_SEARCH_INTEREST";

export const EVIDENCE_CLASS: Record<EvidenceClass, { label: string; tone: Tone; description: string }> = {
  GITHUB_ACTIVITY: {
    label: "GitHub activity",
    tone: "info",
    description:
      "Repository metadata and repeated observations from the GitHub API: external developer activity. Not search interest, not search volume.",
  },
  VERIFIED_RELEASE: {
    label: "Verified release",
    tone: "good",
    description:
      "Published on the repository's own GitHub releases page. Tag, version and date come from GitHub; release-note lines are quoted, never summarised.",
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
      "Independent sources outside GitHub (official posts, Hacker News, YouTube video metadata) mentioning the same entity. Reposts count once.",
  },
  DIRECT_YOUTUBE_SEARCH_INTEREST: {
    label: "Direct YouTube Search interest",
    tone: "critical",
    description:
      "Only from a live-verified direct YouTube Search provider. GitHub data never produces this class.",
  },
};

export const EVIDENCE_ORDER: EvidenceClass[] = [
  "GITHUB_ACTIVITY",
  "VERIFIED_RELEASE",
  "KEYWORD_HYPOTHESIS",
  "EXTERNAL_ATTENTION",
  "DIRECT_YOUTUBE_SEARCH_INTEREST",
];

export interface GitHubRepo {
  repository_id: number;
  full_name: string;
  name: string;
  owner_login: string;
  html_url: string;
  description: string | null;
  language: string | null;
  topics: string[];
  category: string;
  strategies: string[];
  created_at: string | null;
  pushed_at: string | null;
  stargazers_count: number;
  forks_count: number;
  relevance: number;
  discovery_score: number;
  quality_flags: string[];
  first_seen_at: string | null;
  last_seen_at: string | null;
  freshness_hours: number;
  source_key: string;
  evidence_class: EvidenceClass;
  synthetic: boolean;
}

export interface GitHubRelease {
  release_id: number;
  repository: string;
  repository_id: number | null;
  tag_name: string;
  name: string | null;
  version: string | null;
  release_level: string;
  prerelease: boolean;
  html_url: string;
  published_at: string | null;
  is_breaking: boolean;
  breaking_change_lines: string[];
  new_capability_lines: string[];
  api_lines: string[];
  flags: string[];
  category: string;
  first_seen_at: string | null;
  evidence_class: EvidenceClass;
  synthetic: boolean;
}

export interface GitHubList<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  demo: boolean;
  synthetic: boolean;
  detail?: string;
}

export interface GitHubSummary {
  demo: boolean;
  synthetic: boolean;
  detail?: string;
  repositories: number;
  observations: number;
  releases: number;
  last_observed_at?: string | null;
  freshness_hours?: number | null;
  provider: { status: string; detail?: string; auth_mode?: string | null; api_version?: string | null };
  signal_label?: string;
}

export interface Momentum {
  signal_class: string;
  signal_label: string;
  status: string;
  observations: number;
  interval_hours: number | null;
  star_delta: number | null;
  fork_delta: number | null;
  stars_per_day: number | null;
  forks_per_day: number | null;
  repository_age_days: number | null;
  days_since_release: number | null;
  independent_corroborations: number;
  notes: string[];
  caveats: string[];
}

export interface KeywordCandidate {
  keyword_id: number;
  query: string;
  origin: string;
  method: string;
  template: string | null;
  intent: string;
  status: string;
  priority: number;
  evidence_class: EvidenceClass;
}

export interface GitHubRepoDetail extends GitHubRepo {
  entity: { id: number; name: string; type: string; status: string } | null;
  momentum: Momentum;
  releases: GitHubRelease[];
  keyword_candidates: KeywordCandidate[];
  external_attention: { evidence_class: EvidenceClass; independent_publishers: number; providers: string[]; basis: string };
  direct_youtube_search_interest: { evidence_class: EvidenceClass; available: boolean; detail: string };
  observations: { observed_at: string; stargazers_count: number; forks_count: number; strategy: string }[];
}
