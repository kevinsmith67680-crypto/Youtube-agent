import { describe, expect, it } from "vitest";

import { decide, safeEqual } from "@/lib/auth";

const basic = (u: string, p: string) => "Basic " + btoa(`${u}:${p}`);
const prod = {
  NODE_ENV: "production",
  DASHBOARD_BASIC_AUTH_USER: "creator",
  DASHBOARD_BASIC_AUTH_PASSWORD: "a-long-random-password-123",
};

describe("dashboard authentication", () => {
  it("refuses to serve in production without credentials", () => {
    const d = decide({ NODE_ENV: "production" }, null);
    expect(d.kind).toBe("misconfigured");
    // the "disabled" switch is ignored in production
    expect(decide({ NODE_ENV: "production", DASHBOARD_AUTH: "disabled" }, null).kind).toBe(
      "misconfigured",
    );
  });

  it("rejects a short production password", () => {
    const d = decide({ ...prod, DASHBOARD_BASIC_AUTH_PASSWORD: "short" }, basic("creator", "short"));
    expect(d.kind).toBe("misconfigured");
  });

  it("challenges missing, malformed and wrong credentials", () => {
    expect(decide(prod, null).kind).toBe("challenge");
    expect(decide(prod, "Bearer abc").kind).toBe("challenge");
    expect(decide(prod, "Basic !!!not-base64").kind).toBe("challenge");
    expect(decide(prod, basic("creator", "wrong-password-xxxxxxx")).kind).toBe("challenge");
    expect(decide(prod, basic("someone", prod.DASHBOARD_BASIC_AUTH_PASSWORD)).kind).toBe("challenge");
  });

  it("allows the configured user", () => {
    expect(decide(prod, basic("creator", prod.DASHBOARD_BASIC_AUTH_PASSWORD)).kind).toBe("allow");
  });

  it("allows local development without credentials", () => {
    expect(decide({ NODE_ENV: "development" }, null).kind).toBe("allow");
  });

  it("compares in constant structure", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
  });
});
