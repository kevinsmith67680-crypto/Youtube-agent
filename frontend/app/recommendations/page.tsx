import type { Metadata } from "next";

import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm, type Field } from "@/components/FilterForm";
import { GenerateForm } from "@/components/GenerateForm";
import { Pagination } from "@/components/Pagination";
import { RecommendationTable } from "@/components/Tables";
import { Card, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { pageNumber, pick, type SearchParams } from "@/lib/params";
import type { Health, Page, RecommendationSummary } from "@/lib/types";

export const metadata: Metadata = { title: "Recommendations" };
export const dynamic = "force-dynamic";
const PAGE_SIZE = 20;
const KEYS = [
  "category",
  "status",
  "urgency",
  "min_score",
  "q",
  "include_research",
  "include_suppressed",
  "include_dismissed",
  "sort",
  "order",
  "page",
] as const;

const FIELDS: Field[] = [
  { kind: "text", name: "q", label: "Search", placeholder: "title or query" },
  { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
  {
    kind: "select",
    name: "status",
    label: "Verification",
    options: [
      { value: "", label: "Any status" },
      { value: "APPROVED", label: "Approved" },
      { value: "APPROVED_WITH_UNCERTAINTY", label: "Approved with uncertainty" },
      { value: "RESEARCH_REQUIRED", label: "Research required" },
      { value: "REJECTED", label: "Rejected" },
    ],
  },
  {
    kind: "select",
    name: "urgency",
    label: "Urgency",
    options: [
      { value: "", label: "Any urgency" },
      { value: "PUBLISH_SOON", label: "Publish soon" },
      { value: "WITHIN_DAYS", label: "Within days" },
      { value: "FLEXIBLE", label: "Flexible" },
      { value: "UNKNOWN", label: "Unknown" },
      { value: "LIKELY_TOO_LATE", label: "Likely too late" },
    ],
  },
  { kind: "number", name: "min_score", label: "Minimum score", min: 0, max: 100 },
  {
    kind: "select",
    name: "sort",
    label: "Sort by",
    options: [
      { value: "rank", label: "Rank" },
      { value: "score", label: "Score" },
      { value: "hours", label: "Production hours" },
      { value: "generated", label: "Newest" },
      { value: "title", label: "Title" },
    ],
  },
  { kind: "checkbox", name: "include_research", label: "Include research candidates" },
  { kind: "checkbox", name: "include_suppressed", label: "Include suppressed" },
  { kind: "checkbox", name: "include_dismissed", label: "Include dismissed" },
];

export default async function RecommendationsPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, KEYS);
  const page = pageNumber(p.page);
  const [res, health] = await Promise.all([
    apiGet<Page<RecommendationSummary>>("/recommendations", {
      sort: "rank",
      ...p,
      page,
      page_size: PAGE_SIZE,
    }),
    apiGet<Health>("/health"),
  ]);
  const canWrite = health.ok && health.data.auth === "configured";
  return (
    <>
      <PageHeader title="Recommendations">
        Video ideas for verified opportunities, ranked by a transparent, versioned score. Research
        candidates and suppressed duplicates are hidden unless you include them.
      </PageHeader>
      <Card title="Generate" className="mb-4">
        <GenerateForm canWrite={canWrite} />
      </Card>
      <FilterForm action="/recommendations" fields={FIELDS} values={p} />
      {res.ok ? (
        <>
          <RecommendationTable items={res.data.items} caption="Recommendations" />
          <Pagination
            basePath="/recommendations"
            params={p}
            page={page}
            pageSize={PAGE_SIZE}
            total={res.data.total}
          />
        </>
      ) : (
        <ErrorState {...res} what="recommendations" />
      )}
    </>
  );
}
