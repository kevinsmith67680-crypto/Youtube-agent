import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ModeBanner } from "@/components/ModeBanner";
import type { OperatingMode } from "@/lib/types";

const proxy: OperatingMode = {
  mode: "DEGRADED",
  base_mode: "PROXY_OPPORTUNITY_RESEARCH",
  demo: false,
  direct_search_claims_enabled: false,
  reasons: ["no search-interest provider is usable", "degraded: no_live_workers"],
  degradations: [{ kind: "no_live_workers", detail: "no worker heartbeat" }],
  signal_classes_usable: ["OFFICIAL_EVENT_SIGNAL"],
  signal_classes_verified: [],
  next_mode_requirements: [],
};

describe("ModeBanner", () => {
  it("states the mode, the base mode and that direct claims are disabled", () => {
    render(<ModeBanner mode={proxy} />);
    expect(screen.getByRole("status", { name: "Operating mode" })).toBeTruthy();
    expect(screen.getByText("Degraded")).toBeTruthy();
    expect(screen.getByText(/base: Proxy opportunity research/)).toBeTruthy();
    expect(screen.getByText("disabled")).toBeTruthy();
    expect(screen.getByText(/research hypotheses, not measured search demand/)).toBeTruthy();
    expect(screen.getByText("Proxy signal:")).toBeTruthy();
    expect(screen.getByText("Synthetic demo:")).toBeTruthy();
  });

  it("never hides an unknown mode", () => {
    render(<ModeBanner mode={null} />);
    expect(screen.getByText(/Operating mode unknown/)).toBeTruthy();
  });

  it("shows demonstration mode as synthetic", () => {
    render(<ModeBanner mode={{ ...proxy, mode: "DEMONSTRATION", base_mode: "DEMONSTRATION", demo: true }} />);
    expect(screen.getByText("Demonstration")).toBeTruthy();
    expect(screen.getByText(/Synthetic data only/)).toBeTruthy();
  });
});
