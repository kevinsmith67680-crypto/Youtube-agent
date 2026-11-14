import type { Metadata } from "next";
import Link from "next/link";

import { ErrorState } from "@/components/ErrorState";
import { EvidenceClassBadge, MomentumPanel, ReleaseTable } from "@/components/GitHubViews";
import { Badge, Card, EmptyState, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize, safeUrl } from "@/lib/format";
import { CATEGORY_LABEL } from "@/lib/labels";
import type { GitHubRepoDetail } from "@/lib/github";

export const metadata: Metadata = { title: "GitHub repository" };
export const dynamic = "force-dynamic";

export default async function GitHubRepoPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,12}$/.test(id)) {
    return <ErrorState status={404} error="not_found" detail="no such repository" what="repository" />;
  }
  const res = await apiGet<GitHubRepoDetail>(`/github/repositories/${id}`);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="GitHub repository" />
        <ErrorState {...res} what="repository" />
      </>
    );
  }
  const d = res.data;
  const source = safeUrl(d.html_url);
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/github" className="text-accent hover:underline">
          GitHub discoveries
        </Link>
      </nav>
      <PageHeader title={d.full_name}>
        <div className="flex flex-wrap items-center gap-2">
          <EvidenceClassBadge value="GITHUB_ACTIVITY" />
          <Badge>{CATEGORY_LABEL[d.category] ?? humanize(d.category)}</Badge>
          {d.language ? <Badge>{d.language}</Badge> : null}
          {source ? (
            <a href={source} target="_blank" rel="noopener noreferrer nofollow" className="text-sm text-accent hover:underline">
              Source on GitHub
            </a>
          ) : null}
        </div>
        {d.description ? <p className="mt-2">{d.description}</p> : null}
        <p className="mt-1 text-xs">
          Created {date(d.created_at)} · discovered {date(d.first_seen_at)} via {d.source_key} · last observed {date(d.last_seen_at)}
        </p>
      </PageHeader>
      <Card title="Repository attention (external developer activity)" className="mb-4">
        <MomentumPanel m={d.momentum} />
      </Card>
      <Card title="Keyword candidates" description="Generated hypotheses to test, never observed search queries." className="mb-4">
        {d.keyword_candidates.length === 0 ? (
          <EmptyState title="No candidates">The entity was not verified or no template was grounded in the evidence.</EmptyState>
        ) : (
          <ul className="flex flex-col gap-2">
            {d.keyword_candidates.map((k) => (
              <li key={k.keyword_id} className="flex flex-wrap items-center gap-2 text-sm">
                <EvidenceClassBadge value="KEYWORD_HYPOTHESIS" />
                <span className="font-medium">{k.query}</span>
                <span className="text-ink-2">
                  {humanize(k.intent)} · {humanize(k.status)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Card title="External attention">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <EvidenceClassBadge value="EXTERNAL_ATTENTION" />
            <span>
              {d.external_attention.independent_publishers} independent publisher
              {d.external_attention.independent_publishers === 1 ? "" : "s"} outside GitHub
              {d.external_attention.providers.length ? ` (${d.external_attention.providers.join(", ")})` : ""}
            </span>
          </div>
          <p className="mt-2 text-xs text-muted">{d.external_attention.basis}</p>
        </Card>
        <Card title="YouTube Search interest">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <EvidenceClassBadge value="DIRECT_YOUTUBE_SEARCH_INTEREST" />
            <span>{d.direct_youtube_search_interest.detail}</span>
          </div>
        </Card>
      </div>
      <Card title="Releases">
        <ReleaseTable items={d.releases} />
      </Card>
    </>
  );
}
