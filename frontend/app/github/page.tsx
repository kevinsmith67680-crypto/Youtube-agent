import type { Metadata } from "next";

import { ErrorState } from "@/components/ErrorState";
import { EvidenceLegend, ReleaseTable, RepositoryTable } from "@/components/GitHubViews";
import { Badge, Card, Notice, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { age, humanize } from "@/lib/format";
import type { GitHubList, GitHubRelease, GitHubRepo, GitHubSummary } from "@/lib/github";

export const metadata: Metadata = { title: "GitHub discoveries" };
export const dynamic = "force-dynamic";

export default async function GitHubPage() {
  const [summary, ai, tools, releases] = await Promise.all([
    apiGet<GitHubSummary>("/github/summary"),
    apiGet<GitHubList<GitHubRepo>>("/github/repositories", { category: "ai", strategy: "emerging", days: 30, order: "score", page_size: 15 }),
    apiGet<GitHubList<GitHubRepo>>("/github/repositories", { category: "technology", strategy: "emerging", days: 30, order: "score", page_size: 15 }),
    apiGet<GitHubList<GitHubRelease>>("/github/releases", { days: 30, page_size: 15 }),
  ]);
  return (
    <>
      <PageHeader title="GitHub discoveries">
        New AI and developer-tool repositories and releases from the GitHub REST API. This is external developer activity:
        star and fork counts are cumulative account actions, not people, and never YouTube search volume or demand.
      </PageHeader>
      {!summary.ok ? (
        <ErrorState {...summary} what="GitHub summary" />
      ) : (
        <>
          {summary.data.demo ? (
            <Notice tone="synthetic" title="Demo mode">
              {summary.data.detail ?? "GitHub discoveries are not part of the synthetic dataset."}
            </Notice>
          ) : null}
          <Card title="Provider status" className="mb-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={summary.data.provider.status === "available" ? "good" : "warning"}>
                {humanize(summary.data.provider.status)}
              </Badge>
              {summary.data.provider.auth_mode ? <span>auth: {humanize(summary.data.provider.auth_mode)}</span> : null}
              {summary.data.provider.api_version ? <span>API version {summary.data.provider.api_version}</span> : null}
              <span>data freshness: {age(summary.data.freshness_hours ?? null)}</span>
            </div>
            {summary.data.provider.detail ? <p className="mt-2 text-sm text-ink-2">{summary.data.provider.detail}</p> : null}
            <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <StatTile label="Repositories" value={summary.data.repositories} />
              <StatTile label="Observations" value={summary.data.observations} hint="repeated metadata snapshots" />
              <StatTile label="Releases" value={summary.data.releases} />
            </dl>
          </Card>
        </>
      )}
      <EvidenceLegend />
      <Card title="Newly discovered AI repositories" description="Created in the last 21 days at discovery time; ranked by discovery score (recency, relevance, description, adoption), which is not demand." className="mb-4">
        {ai.ok ? <RepositoryTable items={ai.data.items} caption="New AI repositories" /> : <ErrorState {...ai} what="AI repositories" />}
      </Card>
      <Card title="Emerging developer tools" className="mb-4">
        {tools.ok ? <RepositoryTable items={tools.data.items} caption="Emerging developer tools" /> : <ErrorState {...tools} what="developer tools" />}
      </Card>
      <Card title="Recent software releases" description="Monitored repositories; release-note lines are quoted, never summarised.">
        {releases.ok ? <ReleaseTable items={releases.data.items} /> : <ErrorState {...releases} what="releases" />}
      </Card>
    </>
  );
}
