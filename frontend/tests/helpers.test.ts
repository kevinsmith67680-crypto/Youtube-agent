import { describe, expect, it } from "vitest";

import { MISSING, date, hours, humanize, pct, safeUrl, score, withParams } from "@/lib/format";
import { pageNumber, pick } from "@/lib/params";
import { MAX_PROXY_BODY, allowed, sameOrigin } from "@/lib/proxy";

const RID = "7c913baa-4dbb-511b-b40a-6ea9399422e4";

describe("formatting never invents numbers", () => {
  it("renders missing values as a marker, not zero", () => {
    expect(score(null)).toBe(MISSING);
    expect(pct(undefined)).toBe(MISSING);
    expect(date(null)).toBe(MISSING);
    expect(score(0)).toBe("0.0"); // a real zero stays zero
  });
  it("formats values", () => {
    expect(humanize("APPROVED_WITH_UNCERTAINTY")).toBe("Approved with uncertainty");
    expect(pct(0.263)).toBe("26%");
    expect(hours(4.5, 9)).toBe("4.5–9 h");
    expect(hours(2, 2)).toBe("2 h");
    expect(date("2026-11-13T12:00:00Z")).toContain("UTC");
  });
  it("only renders http(s) links", () => {
    expect(safeUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("data:text/html,<b>x</b>")).toBeNull();
    expect(safeUrl("not a url")).toBeNull();
    expect(safeUrl(null)).toBeNull();
  });
  it("builds query strings for links", () => {
    expect(withParams({ category: "ai", q: undefined }, { page: 2 })).toBe("?category=ai&page=2");
    expect(withParams({}, {})).toBe("");
  });
});

describe("search params", () => {
  it("keeps only known keys and bounds values", () => {
    const p = pick({ category: "ai", evil: "x", q: ["a", "b"], long: "y" }, ["category", "q"]);
    expect(p).toEqual({ category: "ai", q: "a" });
    expect(pick({ q: "z".repeat(500) }, ["q"]).q).toHaveLength(120);
  });
  it("parses page numbers defensively", () => {
    expect(pageNumber("3")).toBe(3);
    expect(pageNumber("-1")).toBe(1);
    expect(pageNumber("abc")).toBe(1);
    expect(pageNumber(undefined)).toBe(1);
    expect(pageNumber("99999999")).toBe(1);
  });
});

describe("proxy allowlist", () => {
  it("forwards only the dashboard's own mutations", () => {
    expect(allowed("POST", `recommendations/${RID}/save`)).toBe(true);
    expect(allowed("DELETE", `recommendations/${RID}/save`)).toBe(true);
    expect(allowed("POST", `recommendations/${RID}/production`)).toBe(true);
    expect(allowed("POST", "recommendations/generate")).toBe(true);
    expect(allowed("PUT", "profile")).toBe(true);
    expect(allowed("POST", `alerts/${RID}/read`)).toBe(true);
  });
  it("refuses everything else", () => {
    expect(allowed("GET", "profile")).toBe(false);
    expect(allowed("POST", "profile")).toBe(false);
    expect(allowed("DELETE", `recommendations/${RID}/dismiss`)).toBe(false);
    expect(allowed("POST", "recommendations/not-a-uuid/save")).toBe(false);
    expect(allowed("POST", `recommendations/${RID}/save/../../system`)).toBe(false);
    expect(allowed("POST", "system/status")).toBe(false);
    expect(allowed("POST", "")).toBe(false);
  });
  it("requires a same-origin request", () => {
    expect(sameOrigin("http://localhost:3000", "localhost:3000")).toBe(true);
    expect(sameOrigin("https://evil.example", "localhost:3000")).toBe(false);
    expect(sameOrigin(null, "localhost:3000")).toBe(false);
    expect(sameOrigin("garbage", "localhost:3000")).toBe(false);
    expect(MAX_PROXY_BODY).toBeLessThanOrEqual(64 * 1024);
  });
});
