# Intelligence dashboard

`frontend/` — Next.js 15 (App Router) + TypeScript (strict) + Tailwind CSS + Radix UI
primitives (tooltip, tabs) + Recharts. Dark theme by default; a light theme is one click away
and kept in a cookie so the server renders it without a flash. Setup and commands:
`frontend/README.md`.

## How data reaches the page

* Pages are **server components**: they call the dashboard API from the Next.js server
  (`lib/api.ts`, marked `server-only`) with `TREND_AGENT_API_TOKEN` from the server's
  environment. The token is never serialised into HTML or JavaScript (an end-to-end test
  scans every response body and cookie for it).
* Changes made in the browser (save, unsave, dismiss, feedback, production status,
  generate, profile, mark alert read) are sent to this app's own route
  `/api/proxy/[...path]`, which: forwards only an allowlist of method + path patterns,
  refuses cross-origin requests (Origin must equal the host: CSRF protection), requires
  `application/json`, limits bodies to 16 KB, and adds the token on the server.
* Without a configured token the dashboard is a read-only view and says so in a banner.
* If the API is unreachable, every page shows "Dashboard API unavailable" instead of stale
  or estimated values.

## Pages and navigation

Main navigation (with `aria-current`): Overview · Opportunities · Content gaps · Trends ·
Recommendations · Saved ideas · Settings. A skip link precedes it; demo mode and read-only
mode show a banner on every page.

| Page | Content |
|---|---|
| **Overview** `/` | stat tiles (monitored queries, active surges, candidate opportunities, ranked ideas, research candidates); notes; top 5 ideas; ranked ideas by category (bar chart + table); recently analysed topics; provider status; data freshness; unread alerts |
| **Opportunity explorer** `/opportunities` | filters: search text, category, search intent, surge stage, status, minimum score, updated within (hours); sort by score, last updated, surge severity, breakout probability or query; order; pagination. Columns: query, demand evidence (labelled: only direct YouTube Search surges are called YouTube demand), gap and its evidence strength, score, forecast (probability with calibration status, or "No forecast"), approved/total ideas, updated |
| **Opportunity detail** `/opportunities/[id]` | demand statement and limitations (warning when not a YouTube Search measurement), main uncertainty, all content gaps (type, strength, verification status, limitations), forecast tabs, information requirements and coverage, inspected competing videos (by ID, linked to YouTube in a new tab with `rel="noopener noreferrer nofollow"`, with whether a finding rests on metadata or authorised full content), ideas for this opportunity |
| **Content gap explorer** `/gaps` | every gap across opportunities: filters for category, gap type, evidence strength, primary only, include "no verifiable gap"; sort by score, strength, last analysed or query |
| **Trends** `/trends` | monitored queries with their primary measurement, data-class badge, latest value, freshness and surge stage |
| **Trend detail** `/trends/[id]` | data-label legend; one chart per measurement on its own scale; surge episodes shaded; search-interest and proxy-attention forecasts in separate cards; episodes; evidence |
| **Recommendations** `/recommendations` | generate control (category, dry run); filters (text, category, verification status, urgency, minimum score, include research / suppressed / dismissed); sort by rank, score, hours, newest or title |
| **Recommendation detail** `/recommendations/[id]` | status and labels; concept; title options with strategy, length and query coverage; three hooks with type and length; labelled outline with legend, items to verify before publishing, failure points, conclusion to investigate; key questions and information requirements; alternative formats; evidence; **Your decision** (notes, save, dismiss, feedback, production status with history); feasibility with time breakdown; **Why this rank** (component chart + table, explanation, version); demand and forecast; verification record (checks, removed statements, uncertainties) |
| **Saved ideas** `/saved` | saved ideas with notes and production status; filters for production status and category |
| **Settings** `/settings` | creator profile form; system status (mode, version, database schema, LLM mode, token use and budget, versions); alert channels; all alerts with mark-as-read; data quality |

Filters live in the URL (plain GET forms): they work without JavaScript, can be shared and
survive the back button. Pagination links keep the filters.

## Visualisations

Every chart follows the same rules (validated palette: blue / orange / aqua, checked for
colour-vision deficiency in both themes):

* **One measurement per chart, one axis per chart.** Google Web Search interest, YouTube
  Search interest and proxy signals (video activity) are never drawn on one scale; each
  series has its own chart with its provider, search property, unit, scale type,
  granularity, region and freshness in the caption.
* **Data-class badges** on every series and forecast: OBSERVED, FORECAST, PROXY_SIGNAL,
  SYNTHETIC, MISSING — with the definition on hover, on keyboard focus and in the
  accessible name.
* **Forecasts**: the dashed median continues from the last observation, with the interval
  band (its level in the legend), the data cutoff and the breakout threshold marked. When no
  valid numerical forecast exists (insufficient history, unsupported granularity, source
  unavailable, out of distribution, unavailable) **no line is drawn**: an "unavailable"
  state lists the reasons and states that this is not a prediction of zero growth.
* Missing observations stay gaps in the line; a series with none shows "Missing: no
  observations are stored. Nothing is estimated in their place."
* Score breakdown bars show only available components; missing ones are listed as
  "missing, excluded from the score (not counted as zero)".
* Every chart has a **table view** (`Show data table`) and a text summary for screen
  readers; tooltips enhance and never gate a value.

## Accessibility

Landmarks (header, navigation, main, footer), skip link, labelled named regions for every
card, headings in order, table captions and column scopes, labelled form controls, visible
focus rings, status messages in `aria-live` regions, external links announced as opening a
new tab, colour never the only carrier (badges have text and glyphs), reduced-motion
respected, no horizontal scrolling at phone width. Checked automatically with axe-core
(component tests) and axe WCAG 2.1 A/AA scans of every page in both themes (Playwright).

## Tests

`npm test` (Vitest + Testing Library + axe-core), `npx playwright test --trace off`
(end-to-end against the demo API: rendering, filtering and sorting, save / feedback /
production round trip, proxy refusals, token never in the browser, theme, phone layout).
