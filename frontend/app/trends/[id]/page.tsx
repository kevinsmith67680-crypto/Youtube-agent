import type { Metadata } from "next";
import Link from "next/link";

import { DataClassBadge, SyntheticBadge } from "@/components/Badges";
import { ForecastPanel } from "@/components/charts/ForecastPanel";
import { SeriesChart } from "@/components/charts/SeriesChart";
import { ErrorState } from "@/components/ErrorState";
import { Badge, Card, Notice, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { CATEGORY_LABEL, DATA_CLASS } from "@/lib/labels";
import { date, humanize, num, safeUrl } from "@/lib/format";
import type { DataClass, TrendDetail, TrendSeriesView } from "@/lib/types";

export const metadata: Metadata = { title: "Trend detail" };
export const dynamic = "force-dynamic";

const PROXY = new Set(["video_activity", "community_activity", "official_event_signal"]);

function observedFor(series: TrendSeriesView[], proxy: boolean): TrendSeriesView | null {
  return series.find((s) => PROXY.has(s.measurement) === proxy && s.points.some((p) => p.value !== null)) ?? null;
}

export default async function TrendPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const res = await apiGet<TrendDetail>(`/trends/${encodeURIComponent(id)}`);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Trend detail" />
        <ErrorState {...res} what="trend" />
      </>
    );
  }
  const d = res.data;
  const s = d.summary;
  const searchForecasts = d.forecasts.filter((f) => !f.is_proxy);
  const proxyForecasts = d.forecasts.filter((f) => f.is_proxy);
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/trends" className="text-accent hover:underline">
          Trends
        </Link>
      </nav>
      <PageHeader title={s.query}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge>{CATEGORY_LABEL[s.category ?? ""] ?? humanize(s.category)}</Badge>
          {s.surge_stage ? <Badge tone="info">Surge: {humanize(s.surge_stage)}</Badge> : null}
          {s.synthetic ? <SyntheticBadge /> : null}
          {s.opportunity_id ? (
            <Link href={`/opportunities/${s.opportunity_id}`} className="text-sm text-accent hover:underline">
              Open the content opportunity
            </Link>
          ) : null}
        </div>
      </PageHeader>
      <section aria-labelledby="legend-heading" className="mb-4 rounded-xl border border-line bg-surface p-4">
        <h2 id="legend-heading" className="text-sm font-semibold text-ink">
          Data labels
        </h2>
        <dl className="mt-2 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-5">
          {(Object.keys(DATA_CLASS) as DataClass[]).map((k) => (
            <div key={k}>
              <dt>
                <DataClassBadge value={k} />
              </dt>
              <dd className="mt-1 text-ink-2">{DATA_CLASS[k].description}</dd>
            </div>
          ))}
        </dl>
      </section>
      {d.limitations.length ? (
        <div className="mb-4 space-y-2">
          {d.limitations.map((l) => (
            <Notice key={l} tone="neutral">
              {l}
            </Notice>
          ))}
        </div>
      ) : null}
      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="Measurements" description="One chart per source and measurement, each on its own scale." className="xl:col-span-2">
          {d.series.length ? (
            <div className="grid gap-4 lg:grid-cols-2">
              {d.series.map((series) => (
                <SeriesChart key={`${series.series_id}-${series.label}`} series={series} episodes={d.episodes} />
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-2">No series stored for this query.</p>
          )}
        </Card>
        <Card title="Search-interest forecast" description="Relative interest of the measured source, with its uncertainty interval.">
          <ForecastPanel forecasts={searchForecasts} observed={observedFor(d.series, false)} />
        </Card>
        <Card title="Proxy-attention forecast" description="Video activity forecasts are proxy attention, not search demand.">
          <ForecastPanel forecasts={proxyForecasts} observed={observedFor(d.series, true)} />
        </Card>
        <Card title="Surge episodes">
          {d.episodes.length ? (
            <ul className="divide-y divide-line text-sm">
              {d.episodes.map((e) => (
                <li key={`${e.id}-${e.start}`} className="py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{humanize(e.stage)}</span>
                    <Badge>{humanize(e.classification)}</Badge>
                    <span className="text-xs text-ink-2">severity {num(e.severity, 2)}</span>
                    {e.synthetic ? <SyntheticBadge /> : null}
                  </div>
                  <p className="text-xs text-ink-2">
                    {e.start ? date(e.start) : `detected ${date(e.detected_at)}`} → {e.end ? date(e.end) : "ongoing"}
                  </p>
                  <p className="mt-1 text-xs text-ink-2">{e.statement}</p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-2">No surge episodes detected.</p>
          )}
        </Card>
        <Card title="Evidence">
          {d.evidence.length ? (
            <ul className="divide-y divide-line text-sm">
              {d.evidence.slice(0, 12).map((e) => {
                const url = safeUrl(e.url);
                return (
                  <li key={e.evidence_id} className="py-2">
                    <span className="font-medium">{humanize(e.kind)}</span>
                    {e.publisher ? <span className="text-ink-2"> · {e.publisher}</span> : null}
                    {url ? (
                      <>
                        {" · "}
                        <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
                          source<span className="sr-only"> (opens in a new tab)</span>
                        </a>
                      </>
                    ) : null}
                    <p className="text-xs text-muted">{e.evidence_id}</p>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-sm text-ink-2">No evidence records linked to this query yet.</p>
          )}
          {d.evidence.length > 12 ? (
            <p className="mt-2 text-xs text-ink-2">
              {d.evidence.length - 12} more evidence record(s) are listed on the recommendation pages.
            </p>
          ) : null}
        </Card>
      </div>
    </>
  );
}

