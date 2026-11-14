import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const TOKEN = "e2e-dashboard-token-0123456789abcdef";

async function axe(page: Page) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const violations = r.violations.map(
    (v) => `${v.id} (${v.impact}): ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`,
  );
  expect(violations).toEqual([]);
}

async function firstLink(page: Page, prefix: string): Promise<string> {
  const href = await page.locator(`main a[href^="${prefix}"]`).first().getAttribute("href");
  expect(href).toBeTruthy();
  return href!;
}

test.describe("dashboard pages", () => {
  test("overview: dark by default, synthetic labelled, accessible", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto("/");
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
    await expect(page.getByText("Synthetic demonstration.", { exact: false }).first()).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(page.getByRole("heading", { name: "Top recommendations" })).toBeVisible();
    await axe(page);
    expect(errors).toEqual([]);
  });

  test("the API token never reaches the browser", async ({ page }) => {
    const bodies: string[] = [];
    page.on("response", async (r) => {
      if ((r.headers()["content-type"] ?? "").match(/html|javascript|json/)) {
        try {
          bodies.push(await r.text());
        } catch {
          /* redirects etc. */
        }
      }
    });
    for (const path of ["/", "/recommendations", "/settings"]) await page.goto(path);
    const href = await firstLink(page, "/recommendations/");
    await page.goto(href);
    expect(bodies.length).toBeGreaterThan(3);
    for (const b of bodies) expect(b).not.toContain(TOKEN);
    const cookies = await page.context().cookies();
    expect(cookies.map((c) => c.value).join(" ")).not.toContain(TOKEN);
  });

  test("opportunity explorer filters, sorts and paginates via the URL", async ({ page }) => {
    await page.goto("/opportunities");
    const rows = page.locator("main table tbody tr");
    const total = await rows.count();
    expect(total).toBeGreaterThan(1);
    await page.getByLabel("Category").selectOption("gaming");
    await page.getByRole("button", { name: "Apply" }).click();
    await expect(page).toHaveURL(/category=gaming/);
    const gaming = await rows.count();
    expect(gaming).toBeGreaterThan(0);
    expect(gaming).toBeLessThan(total);
    await page.getByLabel("Search query").fill("zzzz-no-such-topic");
    await page.getByRole("button", { name: "Apply" }).click();
    await expect(page.getByText("No opportunities match")).toBeVisible();
    await page.getByRole("link", { name: "Reset" }).click();
    await expect(page).toHaveURL(/\/opportunities$/);
    await axe(page);
  });

  test("opportunity detail shows gaps, inspected videos and safe external links", async ({ page }) => {
    await page.goto("/opportunities");
    await page.goto(await firstLink(page, "/opportunities/"));
    await expect(page.getByRole("heading", { name: "Content gaps" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Demand evidence" })).toBeVisible();
    const external = page.locator('main a[target="_blank"]');
    for (const a of await external.all()) {
      await expect(a).toHaveAttribute("rel", /noopener/);
      expect(await a.getAttribute("href")).toMatch(/^https?:\/\//);
    }
    await axe(page);
  });

  test("content gap explorer filters by strength", async ({ page }) => {
    await page.goto("/gaps");
    await expect(page.getByRole("heading", { level: 1, name: "Content gap explorer" })).toBeVisible();
    const all = await page.locator("main ul > li").count();
    await page.getByLabel("Evidence strength").selectOption("strong");
    await page.getByRole("button", { name: "Apply" }).click();
    await expect(page).toHaveURL(/strength=strong/);
    const strong = await page.locator("main ul > li").count();
    expect(strong).toBeGreaterThan(0);
    expect(strong).toBeLessThanOrEqual(all);
    await expect(page.locator("main").getByText("Strong evidence").first()).toBeVisible();
    await axe(page);
  });

  test("trend detail: separate scales, labelled data classes, forecast or unavailable state", async ({ page }) => {
    await page.goto("/trends");
    await page.goto(await firstLink(page, "/trends/"));
    await expect(page.getByRole("heading", { name: "Data labels" })).toBeVisible();
    const figures = page.locator("main figure");
    expect(await figures.count()).toBeGreaterThanOrEqual(1);
    await expect(page.getByText("One chart per source and measurement, each on its own scale.")).toBeVisible();
    // every forecast card shows either a chart with a table view or an explicit unavailable state
    const proxy = page.getByRole("region", { name: "Proxy-attention forecast" });
    await expect(proxy.getByText(/not a prediction of zero growth|Proxy attention, not search demand/).first()).toBeVisible();
    await expect(page.locator(".recharts-wrapper").first()).toBeVisible();
    await axe(page);
  });

  test("recommendation detail: labelled outline, score breakdown, verification", async ({ page }) => {
    await page.goto("/recommendations");
    await page.goto(await firstLink(page, "/recommendations/"));
    await expect(page.getByRole("heading", { name: "Outline" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Planned demonstration/ }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Why this rank" })).toBeVisible();
    await expect(page.getByText(/not the probability that a video succeeds/)).toBeVisible();
    await expect(page.getByText(/Application code decided this status/)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Opening hooks" })).toBeVisible();
    await axe(page);
  });

  test("save, feedback and production tracking round-trip through the proxy", async ({ page }) => {
    await page.goto("/recommendations");
    const href = await firstLink(page, "/recommendations/");
    await page.goto(href);
    await page.getByLabel("Notes (private)").fill("e2e note");
    await page.getByRole("button", { name: "Save idea" }).click();
    await expect(page.getByText("Save: done.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Remove from saved" })).toBeVisible();
    await page.getByRole("button", { name: /^Useful/ }).click();
    await expect(page.getByText('Feedback "Useful": done.')).toBeVisible();
    await page.getByLabel("Move to").selectOption("SCRIPTING");
    await page.getByRole("button", { name: "Update status" }).click();
    await expect(page.getByText("Production status: done.")).toBeVisible();
    await expect(page.getByText("Production status: Scripting")).toBeVisible();
    await page.goto("/saved");
    await expect(page.getByText("e2e note")).toBeVisible();
    await expect(page.locator("main li").getByText("Scripting").first()).toBeVisible();
    await axe(page);
  });

  test("the proxy refuses cross-origin and unknown requests", async ({ request }) => {
    const rid = "00000000-0000-0000-0000-000000000000";
    const cross = await request.post(`/api/proxy/recommendations/${rid}/save`, {
      headers: { Origin: "https://evil.example", "Content-Type": "application/json" },
      data: {},
    });
    expect(cross.status()).toBe(403);
    const unknown = await request.post("/api/proxy/system/status", {
      headers: { "Content-Type": "application/json" },
      data: {},
    });
    expect(unknown.status()).toBe(404);
  });

  test("settings shows the profile, alert channels and system status", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Creator profile" })).toBeVisible();
    await expect(page.getByLabel("Display name")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Alert channels" })).toBeVisible();
    await expect(page.locator("main").getByText("Unconfigured").first()).toBeVisible();
    await page.getByLabel("Hours available per week").fill("10");
    await page.getByRole("button", { name: "Save profile" }).click();
    await expect(page.getByText(/Profile saved/)).toBeVisible();
    await axe(page);
  });

  test("light theme toggle persists and stays accessible", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Switch to light theme" }).click();
    await expect(page.locator("html")).not.toHaveClass(/dark/);
    await page.reload();
    await expect(page.locator("html")).not.toHaveClass(/dark/);
    await axe(page);
  });

  test("@responsive pages fit a phone screen without horizontal scrolling", async ({ page }) => {
    for (const path of ["/", "/recommendations", "/gaps"]) {
      await page.goto(path);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, path).toBeLessThanOrEqual(1);
    }
  });

  test("the dashboard refuses anonymous visitors in production", async ({ baseURL }) => {
    // plain fetch: Playwright contexts inherit the configured credentials, fetch does not
    const page = await fetch(`${baseURL}/`);
    expect(page.status).toBe(401);
    expect(page.headers.get("www-authenticate")).toContain("Basic");
    const proxy = await fetch(`${baseURL}/api/proxy/recommendations/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all: true }),
    });
    expect(proxy.status).toBe(401);
    expect((await fetch(`${baseURL}/healthz`)).status).toBe(200);
  });

  test("the operating mode is shown on every page, with the signal legend", async ({ page }) => {
    for (const path of ["/", "/opportunities", "/recommendations", "/trends"]) {
      await page.goto(path);
      const banner = page.getByRole("status", { name: "Operating mode" });
      await expect(banner, path).toBeVisible();
      await expect(banner.getByText("Demonstration")).toBeVisible();
      await expect(banner.getByText("disabled")).toBeVisible();
    }
    await page.getByText("Why, and what each label means").click();
    await expect(page.getByText("Proxy signal:")).toBeVisible();
    await page.screenshot({ path: "../docs/screenshots/phase8_mode_banner.png", fullPage: false });
    await axe(page);
  });
});
