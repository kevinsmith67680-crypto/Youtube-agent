// Dashboard initial-load timings with Chromium (Navigation Timing API).
// Usage: node scripts/benchmark_dashboard.cjs http://127.0.0.1:3000
// The app must be the production build (`npm run build && npm start`) against the API.
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  ({ chromium } = require("/opt/node22/lib/node_modules/playwright"));
}

const base = process.argv[2] || "http://127.0.0.1:3000";
const RUNS = Number(process.env.RUNS || 5);

async function measure(page, url) {
  await page.goto(url, { waitUntil: "load" });
  return page.evaluate(() => {
    const n = performance.getEntriesByType("navigation")[0];
    const fcp = performance.getEntriesByName("first-contentful-paint")[0];
    return {
      ttfb_ms: n.responseStart - n.startTime,
      dom_content_loaded_ms: n.domContentLoadedEventEnd - n.startTime,
      load_ms: n.loadEventEnd - n.startTime,
      fcp_ms: fcp ? fcp.startTime : null,
      transfer_kb: (n.transferSize || 0) / 1024,
    };
  });
}

function summarise(samples) {
  const out = {};
  for (const key of Object.keys(samples[0])) {
    const vals = samples.map((s) => s[key]).filter((v) => v !== null).sort((a, b) => a - b);
    if (!vals.length) continue;
    out[key] = {
      median: Math.round(vals[Math.floor(vals.length / 2)] * 10) / 10,
      max: Math.round(vals[vals.length - 1] * 10) / 10,
    };
  }
  return out;
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  const page = await ctx.newPage();
  // resolve detail pages from the lists
  await page.goto(`${base}/recommendations`);
  const rec = await page.locator('main a[href^="/recommendations/"]').first().getAttribute("href");
  await page.goto(`${base}/trends`);
  const trend = await page.locator('main a[href^="/trends/"]').first().getAttribute("href");
  const pages = {
    overview: "/",
    opportunities: "/opportunities",
    opportunities_filtered: "/opportunities?category=gaming&sort=breakout",
    gaps: "/gaps",
    trend_detail: trend,
    recommendations: "/recommendations",
    recommendation_detail: rec,
    settings: "/settings",
  };
  const result = { base, runs: RUNS, browser: "chromium (headless)", pages: {} };
  for (const [name, path] of Object.entries(pages)) {
    const cold = await (async () => {
      const fresh = await browser.newContext();
      const p = await fresh.newPage();
      const m = await measure(p, base + path);
      await fresh.close();
      return m;
    })();
    const warm = [];
    for (let i = 0; i < RUNS; i++) warm.push(await measure(page, base + path));
    result.pages[name] = { cold_first_visit: cold, warm: summarise(warm) };
  }
  await browser.close();
  process.stdout.write(JSON.stringify(result, null, 2));
})().catch((e) => {
  process.stderr.write(String(e && e.stack ? e.stack : e));
  process.exit(1);
});
