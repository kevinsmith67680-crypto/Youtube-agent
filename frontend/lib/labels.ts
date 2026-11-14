import type { DataClass, RecStatus, StatementLabel } from "./types";

export const DATA_CLASS: Record<DataClass, { label: string; description: string }> = {
  OBSERVED: {
    label: "Observed",
    description:
      "Measured by the named provider for this source and search property. A relative index of that source only, not a number of searches.",
  },
  FORECAST: {
    label: "Forecast",
    description:
      "Model estimate of future relative interest with an uncertainty interval. Not an observation and not a prediction of views.",
  },
  PROXY_SIGNAL: {
    label: "Proxy signal",
    description:
      "Indirect attention (video publication or community activity). Not search demand; shown on its own scale.",
  },
  SYNTHETIC: {
    label: "Synthetic",
    description: "Fictional demonstration or test data. It says nothing about the real world.",
  },
  MISSING: {
    label: "Missing",
    description: "No usable data is stored. Nothing has been estimated in its place.",
  },
};

export const STATEMENT: Record<StatementLabel, { label: string; description: string; glyph: string }> = {
  VERIFIED_FACT: {
    label: "Verified fact",
    glyph: "✓",
    description: "Supported by cited evidence that passed verification.",
  },
  PLANNED_DEMONSTRATION: {
    label: "Planned demonstration",
    glyph: "▶",
    description: "Something the creator will show or do on camera. Not a claim about results.",
  },
  HYPOTHESIS_TO_TEST: {
    label: "Hypothesis to test",
    glyph: "?",
    description: "A question the video investigates. The result is unknown until filmed.",
  },
  UNKNOWN: {
    label: "Unknown",
    glyph: "!",
    description: "Not established by the evidence. Research it before filming or publishing.",
  },
};

export const REC_STATUS: Record<RecStatus, { label: string; glyph: string; tone: Tone; description: string }> = {
  APPROVED: {
    label: "Approved",
    glyph: "✓",
    tone: "good",
    description: "Every factual element passed application-level verification.",
  },
  APPROVED_WITH_UNCERTAINTY: {
    label: "Approved with uncertainty",
    glyph: "◐",
    tone: "warning",
    description: "Approved; some elements were removed or labelled uncertain. Read the uncertainties.",
  },
  RESEARCH_REQUIRED: {
    label: "Research required",
    glyph: "?",
    tone: "info",
    description: "Not enough verified evidence to recommend filming. A research candidate, not ranked.",
  },
  REJECTED: {
    label: "Rejected",
    glyph: "✕",
    tone: "critical",
    description: "Contradicted by evidence. Not recommended.",
  },
};

export type Tone = "neutral" | "info" | "good" | "warning" | "critical" | "synthetic";

export const URGENCY: Record<string, string> = {
  PUBLISH_SOON: "Publish soon",
  WITHIN_DAYS: "Within days",
  FLEXIBLE: "Flexible",
  LIKELY_TOO_LATE: "Likely too late",
  UNKNOWN: "Unknown (no estimable window)",
};

export const PRODUCTION_FLOW: Record<string, string[]> = {
  IDEA: ["RESEARCHING", "SCRIPTING", "ABANDONED"],
  RESEARCHING: ["IDEA", "SCRIPTING", "ABANDONED"],
  SCRIPTING: ["RESEARCHING", "RECORDING", "ABANDONED"],
  RECORDING: ["SCRIPTING", "EDITING", "ABANDONED"],
  EDITING: ["RECORDING", "READY_TO_PUBLISH", "ABANDONED"],
  READY_TO_PUBLISH: ["EDITING", "PUBLISHED", "ABANDONED"],
  PUBLISHED: [],
  ABANDONED: ["IDEA"],
};

export const FEEDBACK_CHOICES: { value: string; label: string }[] = [
  { value: "USEFUL", label: "Useful" },
  { value: "NOT_RELEVANT", label: "Not relevant" },
  { value: "TOO_COMPETITIVE", label: "Too competitive" },
  { value: "TOO_LATE", label: "Too late" },
  { value: "UNVERIFIED", label: "Seems unverified" },
];

export const CATEGORIES = ["ai", "technology", "gaming"] as const;
export const CATEGORY_LABEL: Record<string, string> = {
  ai: "Artificial intelligence",
  technology: "Technology",
  gaming: "Gaming",
};

export const VIDEO_FORMATS = [
  "HANDS_ON_AI_EXPERIMENT",
  "AI_AGENT_TUTORIAL",
  "AI_PRODUCT_DEMONSTRATION",
  "AI_MODEL_COMPARISON",
  "BENCHMARK_INVESTIGATION",
  "AI_AUTOMATION_CHALLENGE",
  "BUILD_AN_APP_TUTORIAL",
  "AI_TOOL_REVIEW",
  "LOCAL_AI_INSTALLATION_GUIDE",
  "AI_CAPABILITY_INVESTIGATION",
  "PRODUCT_COMPARISON",
  "HARDWARE_REVIEW",
  "PROGRAMMING_TUTORIAL",
  "TECHNICAL_EXPERIMENT",
  "DEVELOPER_TOOL_WALKTHROUGH",
  "TECHNOLOGY_EXPLANATION",
  "SOFTWARE_MIGRATION_GUIDE",
  "CYBERSECURITY_DEMONSTRATION",
  "HARDWARE_BENCHMARK",
  "PRACTICAL_BUILD_CHALLENGE",
  "LIMITED_TIME_EVENT_GUIDE",
  "ITEM_LOCATION_WALKTHROUGH",
  "NEW_SEASON_EXPLANATION",
  "CHALLENGE_COMPLETION_GUIDE",
  "NEW_GAME_REVIEW",
  "GAMEPLAY_STRATEGY",
  "PERFORMANCE_OPTIMIZATION_GUIDE",
  "GAME_UPDATE_BREAKDOWN",
  "GAMING_AI_EXPERIMENT",
  "COMPETITIVE_CHALLENGE",
];
