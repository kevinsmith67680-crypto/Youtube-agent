import type { Metadata } from "next";

import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm, type Field } from "@/components/FilterForm";
import { Pagination } from "@/components/Pagination";
import { OpportunityTable } from "@/components/Tables";
import { PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { pageNumber, pick, type SearchParams } from "@/lib/params";
import type { OpportunitySummary, Page } from "@/lib/types";

export const metadata: Metadata = { title: "Opportunity explorer" };
export const dynamic = "force-dynamic";

const KEYS = ["category", "intent", "surge", "status", "min_score", "max_age_hours", "q", "sort", "order", "page"] as const;
const PAGE_SIZE = 20;

const FIELDS: Field[] = [
  { kind: "text", name: "q", label: "Search query", placeholder: "e.g. install" },
  { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
  {
    kind: "select",
    name: "intent",
    label: "Search intent",
    options: [
      { value: "", label: "Any intent" },
      ...["informational", "tutorial", "problem_solving", "comparison", "review", "demonstration", "news", "gaming_guide"].map(
        (v) => ({ value: v, label: v.replace(/_/g, " ") }),
      ),
    ],
  },
  {
    kind: "select",
    name: "surge",
    label: "Surge stage",
    options: [
      { value: "", label: "Any stage" },
      { value: "confirmed", label: "Confirmed" },
      { value: "candidate", label: "Candidate" },
      { value: "watch", label: "Watch" },
      { value: "none", label: "No surge" },
    ],
  },
  {
    kind: "select",
    name: "status",
    label: "Status",
    options: [
      { value: "", label: "Any status" },
      { value: "candidate", label: "Candidate" },
      { value: "insufficient_evidence", label: "Insufficient evidence" },
      { value: "no_opportunity", label: "No opportunity" },
      { value: "blocked_policy", label: "Blocked by policy" },
    ],
  },
  { kind: "number", name: "min_score", label: "Minimum score", min: 0, max: 100 },
  { kind: "number", name: "max_age_hours", label: "Updated within (h)", min: 1, max: 8760 },
  {
    kind: "select",
    name: "sort",
    label: "Sort by",
    options: [
      { value: "score", label: "Opportunity score" },
      { value: "updated", label: "Last updated" },
      { value: "severity", label: "Surge severity" },
      { value: "breakout", label: "Breakout probability" },
      { value: "query", label: "Query" },
    ],
  },
  {
    kind: "select",
    name: "order",
    label: "Order",
    options: [
      { value: "desc", label: "Descending" },
      { value: "asc", label: "Ascending" },
    ],
  },
];

export default async function OpportunitiesPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, KEYS);
  const page = pageNumber(p.page);
  const res = await apiGet<Page<OpportunitySummary>>("/opportunities", { ...p, page, page_size: PAGE_SIZE });
  return (
    <>
      <PageHeader title="Opportunity explorer">
        Content opportunities from Phase 5 analysis, with the demand evidence behind each one.
        Only direct YouTube Search surges are labelled as YouTube demand; external web search and
        proxy signals are labelled as such.
      </PageHeader>
      <FilterForm action="/opportunities" fields={FIELDS} values={p} />
      {res.ok ? (
        <>
          <OpportunityTable items={res.data.items} />
          <Pagination basePath="/opportunities" params={p} page={page} pageSize={PAGE_SIZE} total={res.data.total} />
        </>
      ) : (
        <ErrorState {...res} what="opportunities" />
      )}
    </>
  );
}
