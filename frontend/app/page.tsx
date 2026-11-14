import Link from "next/link";

import { AlertList } from "@/components/AlertList";
import { CategoryBars } from "@/components/charts/CategoryBars";
import { ErrorState } from "@/components/ErrorState";
import { FreshnessTable, ProviderList, RecommendationTable } from "@/components/Tables";
import { Card, Notice, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize } from "@/lib/format";
import type { AlertView, Health, OverviewView } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const [res, health] = await Promise.all([apiGet<OverviewView>("/overview"), apiGet<Health>("/health")]);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Overview" />
        <ErrorState {...res} what="overview" />
      </>
    );
  }
  const canWrite = health.ok && health.data.auth === "configured";
  const alerts = canWrite ? await apiGet<AlertView[]>("/alerts", { unread_only: true }) : null;
  const o = res.data;
  return (
    <>
      <PageHeader title="Overview">
        Monitored demand, verified content opportunities and ranked video ideas. Every number
        comes from stored evidence; unavailable values are shown as unavailable.
      </PageHeader>
      <dl className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Monitored queries" value={o.monitored_queries} />
        <StatTile label="Active surges" value={o.active_surges} hint="candidate or confirmed" />
        <StatTile label="Candidate opportunities" value={o.opportunities} />
        <StatTile label="Ranked ideas" value={o.approved_recommendations} hint="approved, not suppressed" />
        <StatTile label="Research candidates" value={o.research_candidates} hint="need more evidence" />
      </dl>
      {o.notes.length ? (
        <div className="mt-4 space-y-2">
          {o.notes.map((n) => (
            <Notice key={n} tone={o.synthetic ? "synthetic" : "info"}>
              {n}
            </Notice>
          ))}
        </div>
      ) : null}
      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <Card
          title="Top recommendations"
          className="lg:col-span-2"
          actions={
            <Link href="/recommendations" className="text-sm text-accent hover:underline">
              All recommendations
            </Link>
          }
        >
          <RecommendationTable items={o.top_recommendations} caption="Top-ranked recommendations" />
        </Card>
        <Card title="Ranked ideas by category">
          <CategoryBars data={o.category_distribution} />
        </Card>
        <Card title="Recently analysed topics" className="lg:col-span-2">
          {o.recent_topics.length ? (
            <ul className="divide-y divide-line">
              {o.recent_topics.map((t) => (
                <li key={t.opportunity_id} className="flex flex-wrap justify-between gap-2 py-2 text-sm">
                  <Link href={`/opportunities/${t.opportunity_id}`} className="text-accent hover:underline">
                    {t.query}
                  </Link>
                  <span className="text-ink-2">
                    {humanize(t.status)} · {date(t.analysed_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-2">No analysed topics yet.</p>
          )}
        </Card>
        <Card title="Providers">
          <ProviderList items={o.providers} />
        </Card>
        <Card title="Data freshness" className="lg:col-span-2">
          <FreshnessTable items={o.freshness} />
        </Card>
        <Card title="Unread alerts">
          {alerts === null ? (
            <p className="text-sm text-ink-2">Alerts need the API token (private data).</p>
          ) : alerts.ok ? (
            <AlertList alerts={alerts.data.slice(0, 6)} canWrite={canWrite} />
          ) : (
            <ErrorState {...alerts} what="alerts" />
          )}
        </Card>
      </div>
    </>
  );
}
