import type { Metadata } from "next";
import Link from "next/link";

import { DataClassBadge, SyntheticBadge } from "@/components/Badges";
import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm } from "@/components/FilterForm";
import { Pagination } from "@/components/Pagination";
import { EmptyState, PageHeader, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { age, humanize, num } from "@/lib/format";
import { CATEGORY_LABEL } from "@/lib/labels";
import { pageNumber, pick, type SearchParams } from "@/lib/params";
import type { Page, TrendSummary } from "@/lib/types";

export const metadata: Metadata = { title: "Trends" };
export const dynamic = "force-dynamic";
const PAGE_SIZE = 25;

export default async function TrendsPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, ["category", "q", "page"]);
  const page = pageNumber(p.page);
  const res = await apiGet<Page<TrendSummary>>("/trends", { ...p, page, page_size: PAGE_SIZE });
  return (
    <>
      <PageHeader title="Trends">
        Monitored queries and their primary measurement. Values are relative indices of their
        own source; they are not search counts and are not comparable across sources.
      </PageHeader>
      <FilterForm
        action="/trends"
        values={p}
        fields={[
          { kind: "text", name: "q", label: "Search query" },
          { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
        ]}
      />
      {!res.ok ? (
        <ErrorState {...res} what="trends" />
      ) : res.data.items.length === 0 ? (
        <EmptyState title="No monitored queries">Phase 3 monitoring has not stored any targets yet.</EmptyState>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className={tableClass}>
              <caption className="sr-only">Monitored queries</caption>
              <thead>
                <tr>
                  <th scope="col" className={thClass}>Query</th>
                  <th scope="col" className={thClass}>Measurement</th>
                  <th scope="col" className={thClass}>Latest value</th>
                  <th scope="col" className={thClass}>Freshness</th>
                  <th scope="col" className={thClass}>Surge stage</th>
                </tr>
              </thead>
              <tbody>
                {res.data.items.map((t) => (
                  <tr key={t.id}>
                    <td className={tdClass}>
                      <Link href={`/trends/${encodeURIComponent(t.id)}`} className="font-medium text-accent hover:underline">
                        {t.query}
                      </Link>
                      <div className="mt-1 flex gap-2 text-xs text-ink-2">
                        {CATEGORY_LABEL[t.category ?? ""] ?? humanize(t.category)}
                        {t.synthetic ? <SyntheticBadge /> : null}
                      </div>
                    </td>
                    <td className={tdClass}>
                      <div className="flex flex-wrap items-center gap-2">
                        {humanize(t.measurement)}
                        <DataClassBadge value={t.data_class} />
                      </div>
                      <div className="text-xs text-ink-2">
                        {t.provider} · {t.granularity}
                      </div>
                    </td>
                    <td className={`${tdClass} tabular`}>{t.latest_value === null ? "missing" : num(t.latest_value)}</td>
                    <td className={tdClass}>{t.latest_at ? `${age(t.freshness_hours)} old` : "no data"}</td>
                    <td className={tdClass}>{t.surge_stage ? humanize(t.surge_stage) : "none"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination basePath="/trends" params={p} page={page} pageSize={PAGE_SIZE} total={res.data.total} />
        </>
      )}
    </>
  );
}
