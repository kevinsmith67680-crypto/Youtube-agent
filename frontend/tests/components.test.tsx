import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { DataClassBadge, RecStatusBadge, StatementBadge } from "@/components/Badges";
import { ForecastPanel, hasNumericForecast } from "@/components/charts/ForecastPanel";
import { ScoreBars } from "@/components/charts/ScoreBars";
import { SeriesChart } from "@/components/charts/SeriesChart";
import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm } from "@/components/FilterForm";
import { Pagination } from "@/components/Pagination";
import { RecommendationActions } from "@/components/RecommendationActions";
import { OpportunityTable, ProviderList, RecommendationTable } from "@/components/Tables";
import { TooltipProvider } from "@/components/ui/InfoTip";

import { forecast, opp, rec, series, user } from "./fixtures";

function ui(node: ReactElement) {
  return render(<TooltipProvider>{node}</TooltipProvider>);
}

async function noViolations(container: HTMLElement) {
  const result = await axe.run(container, {
    rules: {
      "color-contrast": { enabled: false }, // jsdom computes no styles; checked in e2e
      region: { enabled: false }, // components are tested outside page landmarks
    },
  });
  expect(result.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

describe("data labels", () => {
  it("names every data class in text, with its definition for screen readers", () => {
    const { container } = ui(
      <div>
        <DataClassBadge value="OBSERVED" />
        <DataClassBadge value="FORECAST" />
        <DataClassBadge value="PROXY_SIGNAL" />
        <DataClassBadge value="SYNTHETIC" />
        <DataClassBadge value="MISSING" />
      </div>,
    );
    for (const label of ["Observed", "Forecast", "Proxy signal", "Synthetic", "Missing"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeInTheDocument();
    }
    expect(container).toHaveTextContent("Not search demand");
    expect(container).toHaveTextContent("not a number of searches");
  });

  it("status and statement badges carry text and a glyph, not colour alone", () => {
    ui(
      <div>
        <RecStatusBadge value="APPROVED_WITH_UNCERTAINTY" />
        <StatementBadge value="HYPOTHESIS_TO_TEST" />
        <StatementBadge value="UNKNOWN" />
      </div>,
    );
    expect(screen.getByRole("button", { name: /Approved with uncertainty/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hypothesis to test/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Research it before filming/ })).toBeInTheDocument();
  });
});

describe("recommendation and opportunity tables", () => {
  it("renders ranked rows with links, status and effort", async () => {
    const { container } = ui(
      <RecommendationTable items={[rec(), rec({ id: "8c913baa-4dbb-511b-b40a-6ea9399422e4", rank: 2, score: null, status: "RESEARCH_REQUIRED", title: "Research candidate: x" })]} caption="Ideas" />,
    );
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3);
    const first = rows[1]!;
    expect(within(first).getByRole("link", { name: /CodeForge 2/ })).toHaveAttribute(
      "href",
      "/recommendations/7c913baa-4dbb-511b-b40a-6ea9399422e4",
    );
    expect(first).toHaveTextContent("65.4");
    expect(first).toHaveTextContent("5.9–11.8 h");
    expect(rows[2]).toHaveTextContent("—"); // missing score is a marker, never 0
    expect(rows[2]).toHaveTextContent("Research required");
    await noViolations(container);
  });

  it("shows an empty state instead of an empty table", () => {
    ui(<RecommendationTable items={[]} caption="Ideas" />);
    expect(screen.getByText("No recommendations to show")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("labels indirect demand and missing forecasts honestly", () => {
    ui(
      <OpportunityTable
        items={[
          opp({ direct_youtube_search: false, demand_label: "External web-search surge (not YouTube Search)", breakout_probability: null, forecast_status: null }),
        ]}
      />,
    );
    expect(screen.getByText("External web-search surge (not YouTube Search)")).toBeInTheDocument();
    expect(screen.getByText("No forecast")).toBeInTheDocument();
    expect(screen.queryByText("0%")).toBeNull();
  });
});

describe("forecasts", () => {
  it("shows an unavailable state, not a line, when no forecast exists", () => {
    ui(<ForecastPanel forecasts={[]} observed={null} />);
    expect(screen.getByText("No forecast stored")).toBeInTheDocument();
    expect(screen.getByText(/not a prediction of zero growth/)).toBeInTheDocument();
  });

  it("an abstaining forecast is reported with its reasons and no chart", () => {
    const f = forecast({
      status: "INSUFFICIENT_HISTORY",
      status_reasons: ["only 9 hourly observations"],
      trajectory: [],
      breakout_probability: null,
    });
    expect(hasNumericForecast(f)).toBe(false);
    const { container } = ui(<ForecastPanel forecasts={[f]} observed={series()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Forecast unavailable: Insufficient history");
    expect(screen.getByText("only 9 hourly observations")).toBeInTheDocument();
    expect(container.querySelector(".recharts-wrapper")).toBeNull();
    expect(screen.queryByText("Show data table")).toBeNull();
  });

  it("a usable forecast lists probability with its calibration and a data table", async () => {
    const { container } = ui(<ForecastPanel forecasts={[forecast(), forecast({ prediction_id: "x2", horizon_hours: 72 })]} observed={series()} />);
    expect(hasNumericForecast(forecast())).toBe(true);
    expect(screen.getByRole("tab", { name: "24 h" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("26%")).toBeInTheDocument();
    expect(screen.getByText("(Calibrated)")).toBeInTheDocument();
    expect(screen.getByText("Show data table")).toBeInTheDocument();
    expect(container).toHaveTextContent("not available"); // continuation probability missing
    await noViolations(container);
  });
});

describe("series charts", () => {
  it("states missing data instead of drawing anything", () => {
    ui(<SeriesChart series={series({ points: [], data_class: "MISSING" })} episodes={[]} />);
    expect(screen.getByText(/Missing: no observations are stored/)).toBeInTheDocument();
  });

  it("labels the measurement and offers a table view", () => {
    const { container } = ui(<SeriesChart series={series()} episodes={[]} />);
    expect(screen.getByRole("heading", { name: /Direct youtube search interest/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Synthetic/ })).toBeInTheDocument();
    expect(container).toHaveTextContent("2 values from");
    expect(screen.getByText("Show data table")).toBeInTheDocument();
  });
});

describe("score breakdown", () => {
  it("lists missing components as excluded, never as zero bars", () => {
    ui(
      <ScoreBars
        components={[
          { name: "demand_evidence", value: 0.75, weight: 0.25, contribution: 18.8, available: true, raw: {}, note: "" },
          { name: "forecast_opportunity", value: null, weight: 0.15, contribution: null, available: false, raw: {}, note: "no usable forecast" },
        ]}
      />,
    );
    expect(screen.getByText(/missing, excluded from the score \(not counted as zero\)/)).toBeInTheDocument();
  });
});

describe("states and controls", () => {
  it("explains an unreachable API (provider unavailable UI)", () => {
    ui(<ErrorState status={503} error="api_unreachable" detail="The dashboard API is not reachable." />);
    expect(screen.getByText("Dashboard API unavailable")).toBeInTheDocument();
    expect(screen.getByText(/No data is shown rather than stale or estimated values/)).toBeInTheDocument();
  });

  it("explains a missing token without exposing one", () => {
    const { container } = ui(<ErrorState status={401} error="unauthorized" detail="missing token" />);
    expect(container).toHaveTextContent("TREND_AGENT_API_TOKEN");
    expect(container).toHaveTextContent("never sent to the browser");
  });

  it("provider list shows unconfigured providers as such", () => {
    ui(
      <ProviderList
        items={[
          { name: "youtube_data_api", kind: "metadata", status: "unconfigured", configured: false, detail: "no key", last_success: null },
          { name: "google_trends", kind: "search_interest", status: "unavailable", configured: true, detail: "3 failures", last_success: null },
        ]}
      />,
    );
    expect(screen.getByText("Unconfigured")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("the filter form is a labelled GET form keeping current values", async () => {
    const { container } = ui(
      <FilterForm
        action="/opportunities"
        values={{ category: "gaming", q: "lantern", primary_only: "true" }}
        fields={[
          { kind: "text", name: "q", label: "Search query" },
          { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
          { kind: "number", name: "min_score", label: "Minimum score", min: 0, max: 100 },
          { kind: "checkbox", name: "primary_only", label: "Primary only" },
        ]}
      />,
    );
    const form = screen.getByRole("search", { name: "Filters" });
    expect(form).toHaveAttribute("method", "get");
    expect(form).toHaveAttribute("action", "/opportunities");
    expect(screen.getByLabelText("Search query")).toHaveValue("lantern");
    expect(screen.getByLabelText("Category")).toHaveValue("gaming");
    expect(screen.getByLabelText("Primary only")).toBeChecked();
    expect(screen.getByRole("link", { name: "Reset" })).toHaveAttribute("href", "/opportunities");
    await noViolations(container);
  });

  it("pagination keeps filters and announces the range", () => {
    ui(<Pagination basePath="/recommendations" params={{ category: "ai" }} page={2} pageSize={20} total={45} />);
    expect(screen.getByText("Showing 21–40 of 45")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Next" })).toHaveAttribute("href", "/recommendations?category=ai&page=3");
    expect(screen.getByRole("link", { name: "Previous" })).toHaveAttribute("href", "/recommendations?category=ai&page=1");
  });
});

describe("recommendation actions", () => {
  it("are disabled in the read-only view", () => {
    ui(<RecommendationActions id="7c913baa-4dbb-511b-b40a-6ea9399422e4" user={user()} canWrite={false} />);
    expect(screen.getByText(/need the API token/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save idea" })).toBeNull();
  });

  it("save goes to the same-origin proxy as JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(user({ saved: true })), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = ui(<RecommendationActions id="7c913baa-4dbb-511b-b40a-6ea9399422e4" user={user()} canWrite />);
    await userEvent.type(screen.getByLabelText("Notes (private)"), "film friday");
    await userEvent.click(screen.getByRole("button", { name: "Save idea" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/proxy/recommendations/7c913baa-4dbb-511b-b40a-6ea9399422e4/save");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ notes: "film friday" });
    expect(init.headers.Authorization).toBeUndefined(); // the browser never holds the token
    expect(await screen.findByText("Save: done.")).toBeInTheDocument();
    await noViolations(container);
    vi.unstubAllGlobals();
  });

  it("offers only valid production transitions and asks for the video ID", async () => {
    ui(
      <RecommendationActions
        id="7c913baa-4dbb-511b-b40a-6ea9399422e4"
        user={user({
          saved: true,
          production: { status: "READY_TO_PUBLISH", planned_publication_time: null, actual_publication_time: null, published_video_id: null, notes: null, updated_at: "2026-11-13T12:00:00Z", history: [] },
        })}
        canWrite
      />,
    );
    const select = screen.getByLabelText("Move to");
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Editing", "Published", "Abandoned"]);
    await userEvent.selectOptions(select, "PUBLISHED");
    const video = screen.getByLabelText("Published video ID");
    expect(video).toBeRequired();
    expect(video).toHaveAttribute("pattern", "[A-Za-z0-9_-]{11}");
  });

  it("shows the API's refusal (e.g. an invalid transition)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "invalid_transition", detail: "cannot move from IDEA to PUBLISHED", status: 409 }), { status: 409 }),
      ),
    );
    ui(<RecommendationActions id="7c913baa-4dbb-511b-b40a-6ea9399422e4" user={user()} canWrite />);
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(await screen.findByText(/Dismiss failed: cannot move from IDEA to PUBLISHED/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
