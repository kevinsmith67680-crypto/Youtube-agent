import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import {
  ComponentTable,
  FormatList,
  GameTable,
  OpportunityList,
  RankHistory,
  SampleBadge,
  StreamTable,
  TwitchEvidenceBadge,
  TwitchEvidenceLegend,
  ViewerHistory,
} from "@/components/TwitchViews";
import { TooltipProvider } from "@/components/ui/InfoTip";
import { TWITCH_EVIDENCE_ORDER, type TwitchGame, type TwitchGameDetail, type TwitchOpportunity, type TwitchStream } from "@/lib/twitch";

function ui(node: ReactElement) {
  return render(<TooltipProvider>{node}</TooltipProvider>);
}

async function noViolations(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
  expect(result.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

const game: TwitchGame = {
  game_id: "33214",
  name: "Fortnite",
  igdb_id: "1905",
  category_kind: "game",
  latest_rank: 4,
  best_rank: 3,
  rank_observed_at: "2026-11-14T16:13:00+00:00",
  positions_gained: 1,
  window_positions_gained: 1,
  new_in_top: false,
  observed_viewers: 108490,
  observed_streams: 445,
  sample_status: "partial",
  viewers_observed_at: "2026-11-14T16:11:00+00:00",
  viewers_pct_per_hour: 188.4,
  viewer_comparison: "both partial with the same cap (top-N streams vs top-N streams)",
  attention_state: "rising",
  attention_score: 25,
  attention_version: "twitch-attention-v1",
  labels: ["TWITCH_ACTIVITY", "RANK_CHANGE", "OBSERVED_VIEWERS", "PARTIAL_SAMPLE"],
  entity_id: 12,
  promoted_at: null,
  last_seen_at: "2026-11-14T16:13:00+00:00",
  freshness_minutes: 1,
  expires_at: "2026-11-15T16:13:00+00:00",
  in_latest_ranking: true,
  rank_change_since_previous_poll: 1,
};

const stream: TwitchStream = {
  stream_id: "1",
  user_login: "streamer",
  user_name: "Streamer",
  game_id: "33214",
  game_name: "Fortnite",
  title: "<img src=x onerror=alert(1)> ranked",
  language: "en",
  started_at: "2026-11-14T10:00:00+00:00",
  viewer_count: 64418,
  position: 1,
  observed_at: "2026-11-14T16:11:00+00:00",
  channel_url: "https://www.twitch.tv/streamer",
};

describe("Twitch evidence classes", () => {
  it("names all six classes and never presents Twitch as search volume", () => {
    ui(<TwitchEvidenceLegend />);
    for (const label of ["Twitch activity", "Observed viewers", "Partial sample", "Rank change", "Proxy opportunity", "Direct YouTube Search interest"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(TWITCH_EVIDENCE_ORDER).toHaveLength(6);
    expect(screen.getAllByText(/Twitch data never produces this class/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/never a number of searches/).length).toBeGreaterThan(0);
  });

  it("badges are accessible and partial samples are labelled", async () => {
    const { container } = ui(<TwitchEvidenceBadge value="OBSERVED_VIEWERS" />);
    await noViolations(container);
    ui(<SampleBadge status="partial" />);
    expect(screen.getByText("Partial sample")).toBeTruthy();
    ui(<SampleBadge status={null} />);
    expect(screen.getByText("no sample")).toBeTruthy();
  });
});

describe("game table", () => {
  it("shows rank, change, observed viewers with sample status and evidence labels", async () => {
    const { container } = ui(<GameTable items={[game]} caption="Top games" />);
    const row = screen.getByRole("link", { name: "Fortnite" }).closest("tr")!;
    expect(within(row).getByRole("link", { name: "Fortnite" }).getAttribute("href")).toBe("/twitch/33214");
    expect(within(row).getByText("4")).toBeTruthy();
    expect(within(row).getByText("+1")).toBeTruthy();
    expect(within(row).getByText("108,490")).toBeTruthy();
    expect(within(row).getAllByText("Partial sample").length).toBeGreaterThan(0);
    expect(within(row).getByText("Rising")).toBeTruthy();
    expect(within(row).getByText("Rank change")).toBeTruthy();
    expect(container.textContent).not.toMatch(/\d[\d,.]*\s*(searches|search volume)/i);
    expect(container.textContent?.match(/search volume/gi)?.length ?? 0).toBe(container.textContent?.match(/not search volume|never youtube search volume/gi)?.length ?? 0);
    await noViolations(container);
  });

  it("marks games outside the polled ranking and non-game categories", () => {
    ui(
      <GameTable
        items={[{ ...game, game_id: "509658", name: "Just Chatting", category_kind: "non_game", in_latest_ranking: false, observed_viewers: null }]}
        caption="x"
      />,
    );
    expect(screen.getByText("outside top-N")).toBeTruthy();
    expect(screen.getByText("Not a game")).toBeTruthy();
    expect(screen.queryByText("0")).toBeNull(); // absence is never shown as zero viewers
  });

  it("has an empty state", () => {
    const { container } = ui(<GameTable items={[]} caption="x" empty="Nothing measured" />);
    expect(container.textContent).toContain("Nothing measured");
  });
});

describe("streams, histories, components, formats, opportunities", () => {
  it("renders titles as text and channel links safely", async () => {
    const { container } = ui(<StreamTable items={[stream, { ...stream, stream_id: "2", channel_url: "javascript:alert(1)", user_name: "Evil" }]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByText(/<img src=x/).length).toBe(2); // shown as text, twice
    const link = screen.getByRole("link", { name: "Streamer" });
    expect(link.getAttribute("href")).toBe("https://www.twitch.tv/streamer");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByRole("link", { name: "Evil" })).toBeNull();
    expect(screen.getAllByText("64,418").length).toBe(2);
    await noViolations(container);
  });

  it("shows histories with their sample status and basis, and the outside-top-N note", () => {
    const history: TwitchGameDetail["history"] = {
      window_hours: 24,
      ranks: [{ observed_at: "2026-11-14T15:58:00+00:00", rank: 4 }],
      top_games_polls: 3,
      polls_outside_top_n: 2,
      viewers: [
        { observed_at: "2026-11-14T15:59:00+00:00", viewers_observed: 77723, streams_observed: 465, sample_status: "partial", sample_basis: "game_streams", sample_cap: 500, languages: [] },
        { observed_at: "2026-11-14T16:11:00+00:00", viewers_observed: 108490, streams_observed: 445, sample_status: "complete", sample_basis: "top_streams", sample_cap: 300, languages: [] },
      ],
      note: "Polls where the game was outside the polled top-N have no rank (position unknown, not zero viewers).",
    };
    ui(<RankHistory d={history} />);
    expect(screen.getByText(/outside the polled top-N in 2/)).toBeTruthy();
    ui(<ViewerHistory d={history} />);
    expect(screen.getByText("77,723")).toBeTruthy();
    expect(screen.getByText(/game's own stream list \(cap 500\)/)).toBeTruthy();
    expect(screen.getByText(/global top streams \(cap 300\)/)).toBeTruthy();
  });

  it("lists every score component with its basis", () => {
    ui(<ComponentTable components={{ viewer_growth: { value: 1, weight: 0.25, contribution: 25, basis: "188.4 %/h (both partial ...)" }, volatility_penalty: { value: 0, weight: 0.1, contribution: 0, basis: "needs 3 observations" } }} />);
    expect(screen.getByText("Viewer growth")).toBeTruthy();
    expect(screen.getByText(/188.4 %\/h/)).toBeTruthy();
  });

  it("only offers grounded formats and says what the others need", () => {
    ui(
      <FormatList
        formats={[
          { format: "beginner_guide", eligible: true, grounded_by: ["game.beginner"], requires: "verified game entity", queries: ["x beginner guide"] },
          { format: "update_explanation", eligible: false, grounded_by: [], requires: "a verified official update entity", queries: [] },
        ]}
      />,
    );
    expect(screen.getByText(/grounded by game.beginner/)).toBeTruthy();
    expect(screen.getByText(/needs a verified official update entity/)).toBeTruthy();
  });

  it("labels opportunities as proxy hypotheses", () => {
    const opp: TwitchOpportunity = {
      ...game,
      entity: { id: 12, name: "Fortnite", status: "verified", evergreen: true },
      keyword_hypotheses: [{ keyword_id: 1, query: "fortnite beginner guide", template: "game.beginner", intent: "game_guide", status: "prioritized", priority: 70 }],
      format_candidates: [],
      content_gaps: [],
      content_opportunities: [],
      recommendations: [],
      forecasts: [],
    };
    ui(<OpportunityList items={[opp]} />);
    expect(screen.getByText("Proxy opportunity")).toBeTruthy();
    expect(screen.getByText(/Hypotheses to test: fortnite beginner guide/)).toBeTruthy();
    expect(screen.getByText(/evergreen/)).toBeTruthy();
  });
});
