import { defineConfig, devices } from "@playwright/test";

// End-to-end tests run the built dashboard against the API in isolated demo mode
// (synthetic data, in memory; nothing touches a database or an external service).
const API_PORT = Number(process.env.E2E_API_PORT ?? 8010);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3010);
const TOKEN = "e2e-dashboard-token-0123456789abcdef";
const PYTHON_CLI = process.env.E2E_TREND_AGENT ?? "../.venv/bin/trend-agent";
// `next start` runs in production mode, where dashboard authentication is mandatory.
const DASH_USER = "e2e-creator";
const DASH_PASSWORD = "e2e-dashboard-password-0123456789";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "off",
    colorScheme: "dark",
    httpCredentials: { username: DASH_USER, password: DASH_PASSWORD },
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1366, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] }, grep: /@responsive/ },
  ],
  webServer: [
    {
      command: `${PYTHON_CLI} dashboard serve --demo --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      timeout: 300_000,
      reuseExistingServer: false,
      env: {
        TREND_AGENT_API_TOKEN: TOKEN,
        TREND_AGENT_API_CORS_ORIGINS: `http://127.0.0.1:${WEB_PORT}`,
        ...(process.env.TREND_AGENT_DEMO_CACHE_PATH
          ? { TREND_AGENT_DEMO_CACHE_PATH: process.env.TREND_AGENT_DEMO_CACHE_PATH }
          : {}),
      },
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/healthz`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        TREND_AGENT_API_URL: `http://127.0.0.1:${API_PORT}`,
        TREND_AGENT_API_TOKEN: TOKEN,
        DASHBOARD_BASIC_AUTH_USER: DASH_USER,
        DASHBOARD_BASIC_AUTH_PASSWORD: DASH_PASSWORD,
      },
    },
  ],
});
