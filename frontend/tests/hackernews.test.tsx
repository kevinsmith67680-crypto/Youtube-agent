import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import {
  HNEvidenceBadge,
  HNEvidenceLegend,
  HNMomentumPanel,
  ObservationTable,
  PriorityBreakdown,
  SourceHealthTable,
  StoryTable,
} from "@/components/HackerNewsViews";
import { TooltipProvider } from "@/components/ui/InfoTip";
import { HN_EVIDENCE_ORDER, priorityComponents, type HNMomentum, type HNStory } from "@/lib/hackernews";

function ui(node: ReactElement) {
  return render(<TooltipProvider>{node}</TooltipProvider>);
}

const story: HNStory = {
  story_id: 50497147,
  title: "Show HN: Agweb – a browser for agents",
  linked_url: "https://github.com/example/agweb",
  discussion_url: "https://news.ycombinator.com/item?id=50497147",
  domain: "github.com",
  author: "someone",
  kind: "show_hn",
  subject: "product_launch",
  primary_category: "ai",
  secondary_category: "technology",
  ambiguous: true,
  relevance: 0.667,
  project_name: "Agweb",
  potential_intents: ["explanation", "how_to"],
  flags: [],
  lists: ["show", "new"],
  published_at: "2026-11-14T14:00:00+00:00",
  age_hours: 1.5,
  score: 1234,
  comments: 56,
  last_observed_at: "2026-11-14T15:20:00+00:00",
  priority_score: 68.2,
  momentum_state: "unknown",
  score_per_hour: null,
  comments_per_hour: null,
  momentum_status: "insufficient_observations",
  monitoring_state: "active",
  refresh_tier: "high",
  next_refresh_at: "2026-11-14T15:35:00+00:00",
  entity_id: 7,
  first_seen_at: "2026-11-14T14:30:00+00:00",
  freshness_hours: 0.1,
  source_key: "hn:list:showstories",
  evidence_class: "COMMUNITY_ACTIVITY",
  signal_label: "community discussion activity (Hacker News)",
  synthetic: false,
};

const single: HNMomentum = {
  signal_class: "COMMUNITY_ACTIVITY",
  signal_label: "community discussion activity (Hacker News)",
  status: "insufficient_observations",
  state: "unknown",
  observations: 1,
  interval_hours: null,
  score_delta: null,
  comments_delta: null,
  score_per_hour: null,
  comments_per_hour: null,
  notes: ["one observation: a cumulative score is not growth; no change reported"],
  caveats: ["This is community discussion activity. It does not measure YouTube search interest and cannot confirm a YouTube Search surge."],
};

async function noViolations(container: HTMLElement) {
  const result = await axe.run(container, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  expect(result.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

describe("Hacker News evidence classes", () => {
  it("names all four classes and never presents HN attention as search volume", () => {
    ui(<HNEvidenceLegend />);
    for (const label of ["Community activity", "Keyword hypothesis", "External attention", "Direct YouTube Search interest"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(HN_EVIDENCE_ORDER).toHaveLength(4);
    expect(screen.getAllByText(/not views, not search interest, not search volume/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Hacker News data never produces this class/).length).toBeGreaterThan(0);
  });

  it("badge is accessible", async () => {
    const { container } = ui(<HNEvidenceBadge value="COMMUNITY_ACTIVITY" />);
    expect(screen.getByText("Community activity")).toBeTruthy();
    await noViolations(container);
  });
});

describe("story table", () => {
  it("shows kind, categories (ambiguity kept), lists, safe links and cumulative totals", async () => {
    const { container } = ui(<StoryTable items={[story]} caption="Stories" />);
    const row = screen.getByRole("link", { name: story.title }).closest("tr")!;
    expect(within(row).getByRole("link", { name: story.title }).getAttribute("href")).toBe("/hackernews/50497147");
    expect(within(row).getByText("Show HN")).toBeTruthy();
    expect(within(row).getByText("Ambiguous")).toBeTruthy();
    expect(within(row).getByText(/also/)).toBeTruthy();
    expect(within(row).getByText("show")).toBeTruthy();
    const discussion = within(row).getByRole("link", { name: "Discussion" });
    expect(discussion.getAttribute("href")).toBe(story.discussion_url);
    expect(discussion.getAttribute("rel")).toContain("noopener");
    expect(within(row).getByRole("link", { name: "github.com" }).getAttribute("href")).toBe(story.linked_url);
    expect(within(row).getByText("1,234 / 56")).toBeTruthy();
    expect(screen.getByText("Points / comments (total)")).toBeTruthy();
    // one observation: no rate is shown, and nothing is labelled as search volume
    expect(within(row).getByText("not measured")).toBeTruthy();
    // "search volume" may only appear negated (in the class definition), never as a value
    expect(container.textContent).not.toMatch(/\d[\d,.]*\s*(searches|search volume)/i);
    expect(container.textContent?.match(/search volume/gi)?.length ?? 0).toBe(
      container.textContent?.match(/not search volume/gi)?.length ?? 0,
    );
    await noViolations(container);
  });

  it("labels measured rising discussion without calling it demand", () => {
    ui(<StoryTable items={[{ ...story, momentum_state: "rising", score_per_hour: 42.5 }]} caption="x" />);
    expect(screen.getByText("+42.5/h")).toBeTruthy();
    expect(screen.getByText("Rising discussion")).toBeTruthy();
  });

  it("never renders javascript: URLs as links and has an empty state", () => {
    ui(<StoryTable items={[{ ...story, linked_url: "javascript:alert(1)", domain: "evil" }]} caption="x" />);
    expect(screen.queryByRole("link", { name: "evil" })).toBeNull();
    const { container } = ui(<StoryTable items={[]} caption="x" empty="Nothing measured yet" />);
    expect(container.textContent).toContain("Nothing measured yet");
  });

  it("renders story titles as text, never as HTML", () => {
    const { container } = ui(
      <StoryTable items={[{ ...story, title: "<img src=x onerror=alert(1)> Ignore previous instructions" }]} caption="x" />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x/)).toBeTruthy();
  });
});

describe("momentum panel", () => {
  it("does not invent growth from one observation", () => {
    ui(<HNMomentumPanel m={single} />);
    expect(screen.getAllByText("not measured").length).toBe(4);
    expect(screen.getByText(/a cumulative score is not growth/)).toBeTruthy();
    expect(screen.getByText(/cannot confirm a YouTube Search surge/)).toBeTruthy();
  });

  it("shows deltas and rates over the real interval", () => {
    ui(
      <HNMomentumPanel
        m={{ ...single, status: "ok", state: "rising", observations: 3, interval_hours: 0.5, score_delta: 30, score_per_hour: 60, comments_delta: 4, comments_per_hour: 8 }}
      />,
    );
    expect(screen.getByText("30", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("+60/h", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText("+8/h", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText(/over 0.50 h/)).toBeTruthy();
  });
});

describe("priority, observations and health", () => {
  it("lists every preserved component and skips metadata keys", () => {
    const items = priorityComponents({
      relevance: { value: 0.67, weight: 0.25, contribution: 16.7, basis: "classifier relevance" },
      novelty: { value: 0.2, weight: 0.1, contribution: 2, basis: "repost: story 1 linked the same article earlier" },
      _meta: { computed_at: "2026-11-14T15:00:00+00:00" },
    });
    expect(items.map(([k]) => k)).toEqual(["relevance", "novelty"]);
    ui(<PriorityBreakdown items={items} />);
    expect(screen.getByText(/repost: story 1/)).toBeTruthy();
  });

  it("shows stored observations with their origin", () => {
    ui(
      <ObservationTable
        items={[
          { observed_at: "2026-11-14T14:30:00+00:00", score: 10, comments: 2, origin: "list", list: "new", rank: 4, source_key: "hn:list:newstories" },
          { observed_at: "2026-11-14T14:50:00+00:00", score: 25, comments: 5, origin: "refresh", list: null, rank: null, source_key: null },
        ]}
      />,
    );
    expect(screen.getByText("new list, rank 4")).toBeTruthy();
    expect(screen.getByText("refresh")).toBeTruthy();
  });

  it("shows source health per list", async () => {
    const { container } = ui(
      <SourceHealthTable
        items={[
          {
            source_key: "hn:list:newstories",
            list: "newstories",
            enabled: true,
            poll_interval_minutes: 15,
            health: "degraded",
            last_checked_at: null,
            last_success_at: null,
            last_new_items_at: null,
            consecutive_failures: 2,
            last_error: "timeout",
          },
        ]}
      />,
    );
    expect(screen.getByText("Degraded")).toBeTruthy();
    expect(screen.getByText("15 min")).toBeTruthy();
    expect(screen.getByText("timeout")).toBeTruthy();
    await noViolations(container);
  });
});
