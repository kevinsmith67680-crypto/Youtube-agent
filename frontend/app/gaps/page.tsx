import type { Metadata } from "next";
import Link from "next/link";

import { SyntheticBadge } from "@/components/Badges";
import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm, type Field } from "@/components/FilterForm";
import { Pagination } from "@/components/Pagination";
import { Badge, EmptyState, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize, score } from "@/lib/format";
import { pageNumber, pick, type SearchParams } from "@/lib/params";
import type { GapRow, Page } from "@/lib/types";

export const metadata: Metadata = { title: "Content gap explorer" };
export const dynamic = "force-dynamic";
const PAGE_SIZE = 20;
const KEYS = ["category", "gap_type", "strength", "primary_only", "include_unverifiable", "q", "sort", "page"] as const;

const GAP_TYPES = [
  "no_relevant_results",
  "missing_specific_answer",
  "version_mismatch",
  "potentially_outdated",
  "insufficient_practical_demonstration",
  "format_gap",
  "language_or_region_gap",
  "low_relevance",
];

const FIELDS: Field[] = [
  { kind: "text", name: "q", label: "Search", placeholder: "query or statement" },
  { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
  {
    kind: "select",
    name: "gap_type",
    label: "Gap type",
    options: [{ value: "", label: "Any gap type" }, ...GAP_TYPES.map((g) => ({ value: g, label: humanize(g) }))],
  },
  {
    kind: "select",
    name: "strength",
    label: "Evidence strength",
    options: [
      { value: "", label: "Any strength" },
      { value: "strong", label: "Strong" },
      { value: "moderate", label: "Moderate" },
      { value: "weak", label: "Weak" },
    ],
  },
  {
    kind: "select",
    name: "sort",
    label: "Sort by",
    options: [
      { value: "score", label: "Opportunity score" },
      { value: "strength", label: "Evidence strength" },
      { value: "updated", label: "Last analysed" },
      { value: "query", label: "Query" },
    ],
  },
  { kind: "checkbox", name: "primary_only", label: "Primary gaps only" },
  { kind: "checkbox", name: "include_unverifiable", label: "Include 'no verifiable gap'" },
];

const TONE = { strong: "good", moderate: "warning", weak: "neutral" } as const;

export default async function GapsPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, KEYS);
  const page = pageNumber(p.page);
  const res = await apiGet<Page<GapRow>>("/gaps", { ...p, page, page_size: PAGE_SIZE });
  return (
    <>
      <PageHeader title="Content gap explorer">
        Where the inspected YouTube results do not answer what searchers need. Each gap states
        its evidence strength and verification status; a weak gap is a lead to research, not a
        conclusion.
      </PageHeader>
      <FilterForm action="/gaps" fields={FIELDS} values={p} />
      {!res.ok ? (
        <ErrorState {...res} what="content gaps" />
      ) : res.data.items.length === 0 ? (
        <EmptyState title="No content gaps match">Change or clear the filters.</EmptyState>
      ) : (
        <>
          <ul className="space-y-3">
            {res.data.items.map((g, i) => (
              <li key={`${g.opportunity_id}-${g.gap.gap_type}-${i}`} className="rounded-xl border border-line bg-surface p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold">
                    <Link href={`/opportunities/${g.opportunity_id}#gaps`} className="text-accent hover:underline">
                      {g.query}
                    </Link>
                  </h2>
                  <Badge>{humanize(g.gap.gap_type)}</Badge>
                  <Badge tone={TONE[g.gap.evidence_strength as keyof typeof TONE] ?? "neutral"}>
                    {humanize(g.gap.evidence_strength)} evidence
                  </Badge>
                  {g.is_primary ? <Badge tone="info">Primary</Badge> : null}
                  {g.synthetic ? <SyntheticBadge /> : null}
                </div>
                <p className="mt-2 text-sm text-ink">{g.gap.statement}</p>
                <p className="mt-1 text-xs text-ink-2">
                  {g.demand_label} · opportunity score {score(g.opportunity_score)} · {g.recommendation_count} idea(s) ·
                  verification {humanize(g.gap.verification_status).toLowerCase()} · analysed {date(g.analysed_at)}
                </p>
                {g.gap.limitations.length ? (
                  <ul className="mt-1 list-disc pl-5 text-xs text-muted">
                    {g.gap.limitations.map((l) => (
                      <li key={l}>{l}</li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
          <Pagination basePath="/gaps" params={p} page={page} pageSize={PAGE_SIZE} total={res.data.total} />
        </>
      )}
    </>
  );
}
