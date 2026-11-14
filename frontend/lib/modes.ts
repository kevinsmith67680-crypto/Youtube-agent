import type { Tone } from "@/lib/labels";
import type { OperatingModeName } from "@/lib/types";

export const MODE: Record<OperatingModeName, { label: string; tone: Tone; summary: string }> = {
  FULL_SEARCH_INTELLIGENCE: {
    label: "Full search intelligence",
    tone: "good",
    summary:
      "Direct YouTube Search interest is measured by a live-verified provider and surge detection is validated on real labelled surges.",
  },
  LIMITED_SEARCH_INTELLIGENCE: {
    label: "Limited search intelligence",
    tone: "info",
    summary:
      "Search-interest data exists (external search, or direct data not yet validated). Surges are candidates, never confirmed YouTube Search surges.",
  },
  PROXY_OPPORTUNITY_RESEARCH: {
    label: "Proxy opportunity research",
    tone: "warning",
    summary:
      "No search-interest provider is available. Opportunities come from proxy signals (video, community, official events) and are research hypotheses, not measured search demand.",
  },
  DEMONSTRATION: {
    label: "Demonstration",
    tone: "synthetic",
    summary: "Synthetic data only. Nothing shown is live intelligence.",
  },
  DEGRADED: {
    label: "Degraded",
    tone: "critical",
    summary: "Some capabilities are withdrawn because of a failure or an exhausted budget.",
  },
};

// The signal classes a reader may meet on any page, in the order of their evidential weight.
export const SIGNAL_LEGEND: { key: string; label: string; description: string }[] = [
  { key: "REAL", label: "Real data", description: "Collected from a named live provider." },
  { key: "SYNTHETIC", label: "Synthetic demo", description: "Fictional; says nothing about the world." },
  { key: "DIRECT", label: "Direct YouTube Search", description: "Relative interest in YouTube Search itself." },
  { key: "EXTERNAL", label: "External search", description: "Search interest elsewhere (e.g. web search); not YouTube demand." },
  { key: "PROXY", label: "Proxy signal", description: "Video, community or event activity; not search demand." },
  { key: "FORECAST", label: "Forecast", description: "A model estimate with uncertainty; not an observation." },
  { key: "INSUFFICIENT", label: "Insufficient evidence", description: "Not enough evidence to say; nothing is guessed." },
];
