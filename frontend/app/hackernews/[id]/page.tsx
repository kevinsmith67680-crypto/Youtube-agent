import type { Metadata } from "next";
import Link from "next/link";

import { ErrorState } from "@/components/ErrorState";
import {
  CategoryBadges,
  ExternalLink,
  HNEvidenceBadge,
  HNMomentumPanel,
  ObservationTable,
  PriorityBreakdown,
} from "@/components/HackerNewsViews";
import { Badge, Card, EmptyState, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize } from "@/lib/format";
import { KIND_LABEL, priorityComponents, type HNStoryDetail } from "@/lib/hackernews";

export const metadata: Metadata = { title: "Hacker News story" };
export const dynamic = "force-dynamic";

export default async function HackerNewsStoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,12}$/.test(id)) {
    return <ErrorState status={404} error="not_found" detail="no such story" what="story" />;
  }
  const res = await apiGet<HNStoryDetail>(`/hackernews/stories/${id}`);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Hacker News story" />
        <ErrorState {...res} what="story" />
      </>
    );
  }
  const d = res.data;
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/hackernews" className="text-accent hover:underline">
          Hacker News
        </Link>
      </nav>
      <PageHeader title={d.title}>
        <div className="flex flex-wrap items-center gap-2">
          <HNEvidenceBadge value="COMMUNITY_ACTIVITY" />
          <Badge tone="info">{KIND_LABEL[d.kind] ?? humanize(d.kind)}</Badge>
          <Badge>{humanize(d.subject)}</Badge>
          <CategoryBadges s={d} />
          {d.flags.map((f) => (
            <Badge key={f} tone="warning">
              {humanize(f)}
            </Badge>
          ))}
        </div>
        <p className="mt-2 flex flex-wrap gap-3 text-sm">
          <ExternalLink href={d.discussion_url}>Discussion on Hacker News</ExternalLink>
          {d.linked_url ? <ExternalLink href={d.linked_url}>Linked article ({d.domain ?? "external"})</ExternalLink> : null}
        </p>
        <p className="mt-1 text-xs">
          Published {date(d.published_at)} by {d.author ?? "unknown"} · first seen {date(d.first_seen_at)} via {d.source_key} ·
          lists: {d.lists.join(", ") || "—"} · last observed {date(d.last_observed_at)}
        </p>
      </PageHeader>
      <Card title="Activity (community discussion)" description={d.caveat} className="mb-4">
        <dl className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Points (total)" value={d.score} hint="cumulative, latest observation" />
          <StatTile label="Comments (total)" value={d.comments} hint="cumulative, latest observation" />
          <StatTile label="Priority (0–100)" value={Math.round(d.priority_score)} hint={`${d.refresh_tier} refresh tier`} />
          <StatTile label="Observations" value={d.observations.length} hint="stored snapshots" />
        </dl>
        <p className="mb-3 text-xs text-ink-2">
          Monitoring: {humanize(d.monitoring_state)}
          {d.next_refresh_at ? ` · next refresh ${date(d.next_refresh_at)}` : " · no refresh scheduled"}
        </p>
        <HNMomentumPanel m={d.momentum} />
      </Card>
      <Card title="Observations" description="Every stored snapshot; momentum is computed only from these." className="mb-4">
        <ObservationTable items={d.observations} />
      </Card>
      <Card title="Why this priority" description="Configurable heuristic weights; a ranking for attention, not demand." className="mb-4">
        <PriorityBreakdown items={priorityComponents(d.priority_components)} />
      </Card>
      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Card title="Entity">
          {d.entity ? (
            <p className="text-sm">
              <span className="font-medium">{d.entity.name}</span> · {humanize(d.entity.type)} ·{" "}
              <Badge tone={d.entity.status === "verified" ? "good" : "warning"}>{humanize(d.entity.status)}</Badge>
            </p>
          ) : (
            <EmptyState title="No entity linked">Only names in the title from trusted extractors are linked.</EmptyState>
          )}
          {d.project_name ? <p className="mt-2 text-xs text-ink-2">Project named in the title: {d.project_name}</p> : null}
          {d.potential_intents.length ? (
            <p className="mt-2 text-xs text-ink-2">Search intents this kind of story might motivate: {d.potential_intents.map(humanize).join(", ")}</p>
          ) : null}
        </Card>
        <Card title="Other stories linking the same article">
          {d.reposts.length === 0 ? (
            <p className="text-sm text-ink-2">None stored.</p>
          ) : (
            <ul className="text-sm">
              {d.reposts.map((r) => (
                <li key={r.story_id}>
                  <Link href={`/hackernews/${r.story_id}`} className="text-accent hover:underline">
                    Story {r.story_id}
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-xs text-muted">Reposts of one announcement are one origin, never independent confirmation.</p>
        </Card>
      </div>
      <Card title="Keyword candidates" description="Generated hypotheses to test, never observed search queries." className="mb-4">
        {d.keyword_candidates.length === 0 ? (
          <EmptyState title="No candidates">The entity was not verified or no template was grounded in the evidence.</EmptyState>
        ) : (
          <ul className="flex flex-col gap-2">
            {d.keyword_candidates.map((k) => (
              <li key={k.keyword_id} className="flex flex-wrap items-center gap-2 text-sm">
                <HNEvidenceBadge value="KEYWORD_HYPOTHESIS" />
                <span className="font-medium">{k.query}</span>
                <span className="text-ink-2">
                  {humanize(k.intent)} · {humanize(k.status)} · {humanize(k.method)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="External attention">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <HNEvidenceBadge value="EXTERNAL_ATTENTION" />
            <span>
              {d.external_attention.independent_publishers} independent publisher
              {d.external_attention.independent_publishers === 1 ? "" : "s"} outside Hacker News
              {d.external_attention.providers.length ? ` (${d.external_attention.providers.join(", ")})` : ""}
            </span>
          </div>
          <p className="mt-2 text-xs text-muted">{d.external_attention.basis}</p>
        </Card>
        <Card title="YouTube Search interest">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <HNEvidenceBadge value="DIRECT_YOUTUBE_SEARCH_INTEREST" />
            <span>{d.direct_youtube_search_interest.detail}</span>
          </div>
        </Card>
      </div>
    </>
  );
}
