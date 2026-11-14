# Trend Intelligence dashboard (Phase 7)

Next.js 15 (App Router) + TypeScript + Tailwind CSS + Radix UI primitives + Recharts.
Dark theme by default; a light theme is one click away (kept in a cookie).

The dashboard never talks to an external service and never holds a credential in the
browser. Pages are rendered on the Next.js server, which calls the dashboard API
(`trend-agent dashboard serve`) with the API token from its own environment. Changes made in
the browser (save, dismiss, feedback, production status, profile, alert read flags,
generation) go to this app's same-origin proxy route (`app/api/proxy/[...path]`). The proxy
forwards only an allowlist of routes, refuses cross-origin requests (CSRF) and adds the token
on the server.

## Run

```bash
# 1. the API (database mode, or --demo for the isolated synthetic demonstration)
export TREND_AGENT_API_TOKEN=$(openssl rand -hex 32)
trend-agent dashboard serve --demo            # http://127.0.0.1:8000/api

# 2. the dashboard (same token, server-side only)
cd frontend
npm ci
cp .env.example .env.local                    # set TREND_AGENT_API_TOKEN to the same value
npm run build && npm start                    # http://localhost:3000
```

Without a token the API and dashboard run as a read-only view: public reads only, with
every user-specific field removed; saving, feedback, settings and alerts are disabled.

## Pages

| Route | Page |
|---|---|
| `/` | Overview: counts, top ideas, category chart, recent topics, providers, freshness, alerts |
| `/opportunities` | Opportunity explorer: filters, sorting, pagination (all in the URL) |
| `/opportunities/[id]` | Demand evidence, content gaps, requirements, inspected videos, forecast, ideas |
| `/gaps` | Content gap explorer |
| `/trends`, `/trends/[id]` | Trend detail: one chart per measurement, surge episodes, forecasts |
| `/recommendations`, `/recommendations/[id]` | Ranked ideas; full concept with labelled outline, score breakdown, verification |
| `/saved` | Saved ideas with notes and production status |
| `/settings` | Creator profile, alert channels, alerts, system status, data quality |

## Data honesty in the UI

* Every series and forecast carries a data-class badge (OBSERVED, FORECAST, PROXY_SIGNAL,
  SYNTHETIC, MISSING) with its definition available on hover, on keyboard focus and to
  screen readers.
* Each measurement has its own chart and scale: Google Web Search and YouTube Search interest
  are never drawn on one axis. Proxy-attention forecasts are separate from search-interest
  forecasts.
* When no valid numerical forecast exists, an "unavailable" state with the reasons is shown
  instead of a line, and it says that this is not a prediction of zero growth.
* Missing values render as "—" or "missing", never as 0. Scores are labelled as
  research-priority indices, not probabilities.
* Every chart has a table view.

## Tests

```bash
npm run typecheck
npm test                       # Vitest + Testing Library + axe-core (component level)
npx playwright test --trace off  # end-to-end against the demo API, axe WCAG 2.1 AA on every page
```

The end-to-end run starts `../.venv/bin/trend-agent dashboard serve --demo` and `next start`
itself (see `playwright.config.ts`). Set `TREND_AGENT_DEMO_CACHE_PATH` to reuse a built demo
snapshot (the first build takes about a minute).
