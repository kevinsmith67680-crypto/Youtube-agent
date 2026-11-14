import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { EvidenceClassBadge, EvidenceLegend, MomentumPanel, ReleaseTable, RepositoryTable } from "@/components/GitHubViews";
import { TooltipProvider } from "@/components/ui/InfoTip";
import { EVIDENCE_ORDER, type GitHubRelease, type GitHubRepo, type Momentum } from "@/lib/github";

function ui(node: ReactElement) {
  return render(<TooltipProvider>{node}</TooltipProvider>);
}

const repo: GitHubRepo = {
  repository_id: 101,
  full_name: "acme/agentkit",
  name: "agentkit",
  owner_login: "acme",
  html_url: "https://github.com/acme/agentkit",
  description: "A Python framework for building AI agents.",
  language: "Python",
  topics: ["ai-agent", "llm"],
  category: "ai",
  strategies: ["emerging"],
  created_at: "2026-11-08T10:00:00+00:00",
  pushed_at: "2026-11-13T10:00:00+00:00",
  stargazers_count: 1234,
  forks_count: 9,
  relevance: 1,
  discovery_score: 86.4,
  quality_flags: [],
  first_seen_at: "2026-11-14T12:00:00+00:00",
  last_seen_at: "2026-11-14T12:00:00+00:00",
  freshness_hours: 1,
  source_key: "github:search:ai-agent:emerging",
  evidence_class: "GITHUB_ACTIVITY",
  synthetic: false,
};

const release: GitHubRelease = {
  release_id: 7,
  repository: "acme/agentkit",
  repository_id: 101,
  tag_name: "v2.0.0",
  name: "agentkit 2.0.0",
  version: "2.0.0",
  release_level: "major",
  prerelease: false,
  html_url: "https://github.com/acme/agentkit/releases/tag/v2.0.0",
  published_at: "2026-11-12T09:00:00+00:00",
  is_breaking: true,
  breaking_change_lines: ["Removed the deprecated Agent.run_sync method"],
  new_capability_lines: ["Added streaming tool calls"],
  api_lines: [],
  flags: [],
  category: "ai",
  first_seen_at: "2026-11-14T12:00:00+00:00",
  evidence_class: "VERIFIED_RELEASE",
  synthetic: false,
};

const momentum: Momentum = {
  signal_class: "DEVELOPER_ACTIVITY",
  signal_label: "external developer activity",
  status: "insufficient_observations",
  observations: 1,
  interval_hours: null,
  star_delta: null,
  fork_delta: null,
  stars_per_day: null,
  forks_per_day: null,
  repository_age_days: 6,
  days_since_release: null,
  independent_corroborations: 0,
  notes: ["one observation: a cumulative count is not growth; no change reported"],
  caveats: ["This is external developer activity. It does not measure YouTube search interest or search volume."],
};

async function noViolations(container: HTMLElement) {
  const result = await axe.run(container, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  expect(result.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

describe("GitHub evidence classes", () => {
  it("names all five classes in text, distinct from each other", () => {
    ui(<EvidenceLegend />);
    for (const label of [
      "GitHub activity",
      "Verified release",
      "Keyword hypothesis",
      "External attention",
      "Direct YouTube Search interest",
    ]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(EVIDENCE_ORDER).toHaveLength(5);
  });

  it("explains that GitHub activity is not search volume", () => {
    ui(<EvidenceLegend />);
    expect(screen.getAllByText(/Not search interest, not search volume/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/GitHub data never produces this class/).length).toBeGreaterThan(0);
  });

  it("badge has an accessible label", async () => {
    const { container } = ui(<EvidenceClassBadge value="VERIFIED_RELEASE" />);
    expect(screen.getByText("Verified release")).toBeTruthy();
    await noViolations(container);
  });
});

describe("repository table", () => {
  it("shows metadata, a safe source link and the discovery timestamp", async () => {
    const { container } = ui(<RepositoryTable items={[repo]} caption="New AI repositories" />);
    const row = screen.getByRole("link", { name: "acme/agentkit" }).closest("tr")!;
    expect(within(row).getByText("A Python framework for building AI agents.")).toBeTruthy();
    const source = within(row).getByRole("link", { name: "Source on GitHub" });
    expect(source.getAttribute("href")).toBe("https://github.com/acme/agentkit");
    expect(source.getAttribute("rel")).toContain("noopener");
    expect(within(row).getByText("1,234")).toBeTruthy();
    expect(within(row).getByText(/via emerging/)).toBeTruthy();
    expect(within(row).getByText("GitHub activity")).toBeTruthy();
    // a cumulative count is labelled as a total, never as growth or volume
    expect(screen.getByText("Stars (total)")).toBeTruthy();
    // "search volume" may only appear negated (in the class definition), never as a value
    expect(container.textContent).not.toMatch(/\d[\d,.]*\s*(searches|search volume)/i);
    expect(container.textContent?.match(/search volume/gi)?.length ?? 0).toBe(
      container.textContent?.match(/not search volume/gi)?.length ?? 0,
    );
    await noViolations(container);
  });

  it("never renders a javascript: URL from the API as a link", () => {
    ui(<RepositoryTable items={[{ ...repo, html_url: "javascript:alert(1)" }]} caption="x" />);
    expect(screen.queryByRole("link", { name: "Source on GitHub" })).toBeNull();
  });

  it("shows quality flags and an empty state", () => {
    ui(<RepositoryTable items={[{ ...repo, quality_flags: ["star_pattern_unexplained"] }]} caption="x" />);
    expect(screen.getByText("Star pattern unexplained")).toBeTruthy();
    const { container } = ui(<RepositoryTable items={[]} caption="x" />);
    expect(container.textContent).toContain("Nothing discovered yet");
  });
});

describe("release table", () => {
  it("quotes release-note lines and labels the release as verified", async () => {
    const { container } = ui(<ReleaseTable items={[release]} />);
    expect(screen.getByText("Removed the deprecated Agent.run_sync method").tagName).toBe("BLOCKQUOTE");
    expect(screen.getByText("Breaking changes noted")).toBeTruthy();
    expect(screen.getByText("Verified release")).toBeTruthy();
    expect(screen.getByRole("link", { name: /acme\/agentkit v2.0.0/ }).getAttribute("href")).toBe(release.html_url);
    await noViolations(container);
  });
});

describe("momentum panel", () => {
  it("does not invent growth from a single observation", () => {
    ui(<MomentumPanel m={momentum} />);
    expect(screen.getAllByText("not measured").length).toBe(3);
    expect(screen.getByText(/a cumulative count is not growth/)).toBeTruthy();
    expect(screen.getByText(/does not measure YouTube search interest/)).toBeTruthy();
  });

  it("shows deltas over the real interval when measured", () => {
    ui(<MomentumPanel m={{ ...momentum, status: "ok", observations: 2, interval_hours: 48, star_delta: 40, stars_per_day: 20, fork_delta: 3 }} />);
    expect(screen.getByText("40", { selector: "dd" })).toBeTruthy();
    expect(screen.getByText(/^20(\.0)?$/, { selector: "dd" })).toBeTruthy();
    expect(screen.getByText(/over 48(\.0)? h/)).toBeTruthy();
  });
});
