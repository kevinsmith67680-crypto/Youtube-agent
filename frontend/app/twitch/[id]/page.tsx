import type { Metadata } from "next";
import Link from "next/link";

import { ErrorState } from "@/components/ErrorState";
import {
  AttentionBadge,
  ComponentTable,
  FormatList,
  RankHistory,
  SampleBadge,
  TwitchEvidenceBadge,
  ViewerHistory,
} from "@/components/TwitchViews";
import { Badge, Card, EmptyState, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize, safeUrl } from "@/lib/format";
import type { TwitchGameDetail } from "@/lib/twitch";

export const metadata: Metadata = { title: "Twitch game" };
export const dynamic = "force-dynamic";

export default async function TwitchGamePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,20}$/.test(id)) {
    return <ErrorState status={404} error="not_found" detail="no such game" what="game" />;
  }
  const res = await apiGet<TwitchGameDetail>(`/twitch/games/${id}`);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Twitch game" />
        <ErrorState {...res} what="game" />
      </>
    );
  }
  const d = res.data;
  const a = d.attention ?? {};
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/twitch" className="text-accent hover:underline">
          Twitch gaming intelligence
        </Link>
      </nav>
      <PageHeader title={d.name}>
        <div className="flex flex-wrap items-center gap-2">
          <TwitchEvidenceBadge value="TWITCH_ACTIVITY" />
          <AttentionBadge state={d.attention_state} />
          <Badge>{d.category_kind === "game" ? `IGDB ${d.igdb_id}` : d.category_kind === "non_game" ? "Not a game" : "Category unverified"}</Badge>
          {d.monitoring ? (
            <Badge tone="info">
              monitored: {d.monitoring.tier} tier ({d.monitoring.origin})
            </Badge>
          ) : null}
        </div>
        <p className="mt-1 text-xs">
          Twitch id {d.game_id} · last seen {date(d.last_seen_at)} · stored data expires {date(d.expires_at)} (24-hour cache limit)
        </p>
      </PageHeader>
      <Card title="Current observations" description={d.caveat} className="mb-4">
        <dl className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Rank (latest poll)" value={d.latest_rank} hint={d.best_rank ? `best ${d.best_rank} in the window` : undefined} />
          <StatTile label="Observed viewers" value={d.observed_viewers} hint="sum over returned streams" />
          <StatTile label="Observed live streams" value={d.observed_streams} />
          <StatTile label="Attention score (0–100)" value={Math.round(d.attention_score)} hint={d.attention_version ?? undefined} />
        </dl>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <SampleBadge status={d.sample_status} />
          {a.viewers?.comparison ? <span className="text-ink-2">compared: {a.viewers.comparison}</span> : null}
        </div>
        <ul className="mt-2 list-disc pl-5 text-xs text-muted">
          {[...(a.notes ?? []), ...(a.caveats ?? [])].map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </Card>
      <Card title="Why this attention score" description="Interpretable, versioned components; a ranking for attention, not demand." className="mb-4">
        <ComponentTable components={a.components} />
      </Card>
      <div className="mb-4 grid gap-4 xl:grid-cols-2">
        <Card title="Ranking history">
          <div className="mb-2">
            <TwitchEvidenceBadge value="RANK_CHANGE" />
          </div>
          <RankHistory d={d.history} />
        </Card>
        <Card title="Observed-viewer history">
          <div className="mb-2">
            <TwitchEvidenceBadge value="OBSERVED_VIEWERS" />
          </div>
          <ViewerHistory d={d.history} />
        </Card>
      </div>
      <div className="mb-4 grid gap-4 sm:grid-cols-2">
        <Card title="Entity">
          {d.entity ? (
            <p className="text-sm">
              <span className="font-medium">{d.entity.name}</span> · {humanize(d.entity.type)} ·{" "}
              <Badge tone={d.entity.status === "verified" ? "good" : "warning"}>{humanize(d.entity.status)}</Badge>
              {d.entity.evergreen ? <span className="text-ink-2"> · evergreen (expands only on new releases/events)</span> : null}
            </p>
          ) : (
            <EmptyState title="Not sent to discovery yet">Emerging games are verified through Get Games and resolved against existing entities.</EmptyState>
          )}
        </Card>
        <Card title="Possible explanations">
          {d.possible_explanations.length === 0 ? (
            <p className="text-sm text-ink-2">No official announcement or release mentioning this game near the change. Nothing is inferred.</p>
          ) : (
            <ul className="flex flex-col gap-2 text-sm">
              {d.possible_explanations.map((e) => {
                const url = safeUrl(e.url);
                return (
                  <li key={`${e.title}-${e.published_at}`}>
                    {url ? (
                      <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
                        {e.title}
                      </a>
                    ) : (
                      e.title
                    )}
                    <div className="text-xs text-ink-2">
                      {e.publisher ?? "official source"} · {date(e.published_at)} · {e.relationship}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
      <Card title="Keyword hypotheses" description="Generated from the verified game entity; hypotheses to test, never observed search queries." className="mb-4">
        {d.keyword_hypotheses.length === 0 ? (
          <EmptyState title="No hypotheses">The entity is not verified, is evergreen, or the game has not been sent to discovery.</EmptyState>
        ) : (
          <ul className="flex flex-col gap-2">
            {d.keyword_hypotheses.map((k) => (
              <li key={k.keyword_id} className="flex flex-wrap items-center gap-2 text-sm">
                <TwitchEvidenceBadge value="PROXY_OPPORTUNITY" />
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
        <Card title="Candidate video formats">
          <FormatList formats={d.format_candidates} />
        </Card>
        <Card title="Linked pipeline results">
          <ul className="text-sm">
            <li>Phase 5 content gaps: {d.content_gaps.length}</li>
            <li>Phase 5 content opportunities: {d.content_opportunities.length}</li>
            <li>Phase 7 recommendations: {d.recommendations.length}</li>
            <li>Phase 6 forecasts (keyword series, own classification): {d.forecasts.length}</li>
          </ul>
          {d.recommendations.slice(0, 3).map((r) => (
            <p key={r.id} className="mt-2 text-sm">
              <Link href={`/recommendations/${r.id}`} className="text-accent hover:underline">
                {r.title}
              </Link>
              <span className="text-xs text-ink-2"> · {humanize(r.demand_basis)}</span>
            </p>
          ))}
        </Card>
      </div>
      <Card title="YouTube Search interest">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <TwitchEvidenceBadge value="DIRECT_YOUTUBE_SEARCH_INTEREST" />
          <span>{d.direct_youtube_search_interest.detail}</span>
        </div>
      </Card>
    </>
  );
}
