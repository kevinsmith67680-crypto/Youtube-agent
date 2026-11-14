import type { Metadata } from "next";
import { cookies } from "next/headers";
import type { ReactNode } from "react";

import "./globals.css";

import { ModeBanner } from "@/components/ModeBanner";
import { Nav } from "@/components/Nav";
import { ThemeToggle } from "@/components/ThemeToggle";
import { TooltipProvider } from "@/components/ui/InfoTip";
import { Badge } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import type { Health, OperatingMode } from "@/lib/types";

export const metadata: Metadata = {
  title: { default: "Trend Intelligence", template: "%s · Trend Intelligence" },
  description: "Evidence-verified YouTube video recommendations and trend intelligence.",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: ReactNode }) {
  const theme = (await cookies()).get("theme")?.value === "light" ? "light" : "dark";
  const health = await apiGet<Health>("/health");
  const mode = await apiGet<OperatingMode>("/system/mode");
  return (
    <html lang="en-GB" className={theme === "dark" ? "dark" : ""}>
      <body className="min-h-screen">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-surface focus:px-4 focus:py-2"
        >
          Skip to content
        </a>
        <TooltipProvider>
          <header className="border-b border-line bg-surface">
            <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
              <div className="flex items-center gap-3">
                <span className="text-lg font-semibold text-ink">Trend Intelligence</span>
                {health.ok ? (
                  <Badge tone={health.data.synthetic ? "synthetic" : "info"}>
                    {health.data.mode === "demo" ? "Demo mode" : "Live database"}
                  </Badge>
                ) : (
                  <Badge tone="critical">API unavailable</Badge>
                )}
              </div>
              <ThemeToggle initial={theme} />
            </div>
            <div className="mx-auto max-w-7xl px-4 pb-2">
              <Nav />
            </div>
          </header>
          {health.ok ? <ModeBanner mode={mode.ok ? mode.data : null} /> : null}
          {health.ok && health.data.synthetic ? (
            <div role="note" className="border-b border-synthetic/60 bg-synthetic/10">
              <p className="mx-auto max-w-7xl px-4 py-2 text-sm text-ink">
                <strong>Synthetic demonstration.</strong> Fictional products, games and data.
                Nothing on these pages is real search demand or a real recommendation.
              </p>
            </div>
          ) : null}
          {health.ok && health.data.auth === "read-only" ? (
            <div role="note" className="border-b border-warning/60 bg-warning/10">
              <p className="mx-auto max-w-7xl px-4 py-2 text-sm text-ink">
                Read-only view: no API token is configured, so saving ideas, feedback, settings
                and alerts are unavailable.
              </p>
            </div>
          ) : null}
          <main id="main" className="mx-auto max-w-7xl px-4 py-6">
            {children}
          </main>
          <footer className="mx-auto max-w-7xl px-4 pb-8 pt-4 text-xs text-muted">
            Recommendations are ideas to evaluate. Scores rank research priority under
            versioned, experimental weights; they are not probabilities that a video will
            succeed. Nothing is uploaded or published by this system.
          </footer>
        </TooltipProvider>
      </body>
    </html>
  );
}
