import type { Metadata } from "next";

import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm } from "@/components/FilterForm";
import { HNEvidenceLegend, SourceHealthTable, StoryTable } from "@/components/HackerNewsViews";
import { Pagination } from "@/components/Pagination";
import { Badge, Card, Notice, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { age, humanize } from "@/lib/format";
import { LIST_OPTIONS, NOT_SEARCH_DEMAND, type HNList, type HNSummary, type HNTrending } from "@/lib/hackernews";
import { pageNumber, pick, type SearchParams } from "@/lib/params";

export const metadata: Metadata = { title: "Hacker News" };
export const dynamic = "force-dynamic";
const PAGE_SIZE = 25;

const KIND_OPTIONS = [
  { value: "", label: "All kinds" },
  { value: "story", label: "Story" },
  { value: "show_hn", label: "Show HN" },
  { value: "ask_hn", label: "Ask HN" },
  { value: "launch_hn", label: "Launch HN" },
];
const ORDER_OPTIONS = [
  { value: "priority", label: "Priority" },
  { value: "published", label: "Newest" },
  { value: "momentum", label: "Measured momentum" },
  { value: "score", label: "Points (total)" },
  { value: "comments", label: "Comments (total)" },
];
const ALLOWED = {
  list: new Set(LIST_OPTIONS.map((o) => o.value)),
  category: new Set(CATEGORY_OPTIONS.map((o) => o.value)),
  kind: new Set(KIND_OPTIONS.map((o) => o.value)),
  order: new Set(ORDER_OPTIONS.map((o) => o.value)),
};

function only(value: string | undefined, allowed: Set<string>): string | undefined {
  return value && allowed.has(value) ? value : undefined;
}

export default async function HackerNewsPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, ["list", "category", "kind", "order", "page"]);
  const filters = {
    list: only(p.list, ALLOWED.list),
    category: only(p.category, ALLOWED.category),
    kind: only(p.kind, ALLOWED.kind),
    order: only(p.order, ALLOWED.order) ?? "priority",
  };
  const page = pageNumber(p.page);
  const [summary, stories, trending, ai, show, dev, ask] = await Promise.all([
    apiGet<HNSummary>("/hackernews/summary"),
    apiGet<HNList>("/hackernews/stories", { ...filters, days: 7, page, page_size: PAGE_SIZE }),
    apiGet<HNTrending>("/hackernews/trending", { hours: 48, limit: 10 }),
    apiGet<HNList>("/hackernews/stories", { category: "ai", days: 3, order: "priority", page_size: 10 }),
    apiGet<HNList>("/hackernews/stories", { kind: "show_hn", days: 3, order: "priority", page_size: 10 }),
    apiGet<HNList>("/hackernews/stories", { category: "technology", days: 3, order: "priority", page_size: 10 }),
    apiGet<HNList>("/hackernews/stories", { kind: "ask_hn", days: 7, order: "priority", page_size: 10 }),
  ]);
  return (
    <>
      <PageHeader title="Hacker News">
        AI, developer-technology and gaming stories from the official Hacker News API. This is community discussion activity:
        points and comments are cumulative community actions, never YouTube search volume or demand.
      </PageHeader>
      {!summary.ok ? (
        <ErrorState {...summary} what="Hacker News summary" />
      ) : (
        <>
          {summary.data.demo ? (
            <Notice tone="synthetic" title="Demo mode">
              {summary.data.detail ?? "Hacker News stories are not part of the synthetic dataset."}
            </Notice>
          ) : null}
          <Card title="Provider status" className="mb-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={summary.data.provider.status === "available" ? "good" : "warning"}>
                {humanize(summary.data.provider.status)}
              </Badge>
              <span>data freshness: {age(summary.data.freshness_hours ?? null)}</span>
              {summary.data.provider.scheduled_discovery === false ? (
                <span className="text-ink-2">scheduled polling off (TREND_AGENT_SCHEDULED_DISCOVERY)</span>
              ) : null}
            </div>
            {summary.data.provider.detail ? <p className="mt-2 text-sm text-ink-2">{summary.data.provider.detail}</p> : null}
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile label="Stories" value={summary.data.stories} />
              <StatTile label="Monitored" value={summary.data.active_stories ?? 0} hint="refreshed by priority tier" />
              <StatTile label="Observations" value={summary.data.observations} hint="stored snapshots of points/comments" />
              <StatTile label="Rising discussions" value={summary.data.rising_stories ?? 0} hint="measured, community activity" />
            </dl>
          </Card>
        </>
      )}
      <HNEvidenceLegend />
      <FilterForm
        action="/hackernews"
        values={{ ...p, order: filters.order }}
        fields={[
          { kind: "select", name: "list", label: "Source list", options: LIST_OPTIONS },
          { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
          { kind: "select", name: "kind", label: "Kind", options: KIND_OPTIONS },
          { kind: "select", name: "order", label: "Order", options: ORDER_OPTIONS },
        ]}
      />
      <Card title="Stories (last 7 days)" description={NOT_SEARCH_DEMAND} className="mb-4">
        {stories.ok ? (
          <>
            <StoryTable items={stories.data.items} caption="Hacker News stories" />
            <Pagination
              basePath="/hackernews"
              params={{ ...filters }}
              page={page}
              pageSize={PAGE_SIZE}
              total={stories.data.total}
            />
          </>
        ) : (
          <ErrorState {...stories} what="stories" />
        )}
      </Card>
      <Card
        title="Discussion momentum (measured)"
        description="Points per hour between stored observations. Stories seen once are not ranked."
        className="mb-4"
      >
        {trending.ok ? (
          <>
            <StoryTable
              items={trending.data.items}
              caption="Stories with measured discussion momentum"
              empty="No story has two observations far enough apart yet; nothing is estimated."
            />
            {trending.data.awaiting_repeat_observation ? (
              <p className="mt-2 text-xs text-muted">
                {trending.data.awaiting_repeat_observation} more stories await a repeat observation.
              </p>
            ) : null}
          </>
        ) : (
          <ErrorState {...trending} what="momentum" />
        )}
      </Card>
      <div className="mb-4 grid gap-4 xl:grid-cols-2">
        <Card title="AI stories" description="Last 3 days, by priority.">
          {ai.ok ? <StoryTable items={ai.data.items} caption="AI stories" /> : <ErrorState {...ai} what="AI stories" />}
        </Card>
        <Card title="Show HN launches" description="Last 3 days, by priority.">
          {show.ok ? <StoryTable items={show.data.items} caption="Show HN launches" /> : <ErrorState {...show} what="Show HN" />}
        </Card>
        <Card title="Developer technology" description="Last 3 days, by priority.">
          {dev.ok ? <StoryTable items={dev.data.items} caption="Developer technology stories" /> : <ErrorState {...dev} what="developer stories" />}
        </Card>
        <Card title="Ask HN discussions" description="Last 7 days, by priority.">
          {ask.ok ? <StoryTable items={ask.data.items} caption="Ask HN discussions" /> : <ErrorState {...ask} what="Ask HN" />}
        </Card>
      </div>
      <Card title="Provider health" description="One source per official story list, each polled at its own interval.">
        {summary.ok ? <SourceHealthTable items={summary.data.sources} /> : <ErrorState {...summary} what="provider health" />}
      </Card>
    </>
  );
}
