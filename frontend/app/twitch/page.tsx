import type { Metadata } from "next";

import { ErrorState } from "@/components/ErrorState";
import { FilterForm } from "@/components/FilterForm";
import { GameTable, OpportunityList, StreamTable, TwitchEvidenceLegend } from "@/components/TwitchViews";
import { Badge, Card, Notice, PageHeader, StatTile } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize } from "@/lib/format";
import { pick, type SearchParams } from "@/lib/params";
import { NOT_SEARCH_DEMAND, STATE_OPTIONS, type TwitchGames, type TwitchOpportunity, type TwitchStreams, type TwitchSummary } from "@/lib/twitch";

export const metadata: Metadata = { title: "Twitch gaming intelligence" };
export const dynamic = "force-dynamic";

const RANK_OPTIONS = [
  { value: "", label: "Any rank" },
  { value: "10", label: "Top 10" },
  { value: "25", label: "Top 25" },
  { value: "50", label: "Top 50" },
  { value: "100", label: "Top 100" },
];
const PERIOD_OPTIONS = [
  { value: "", label: "Retention window" },
  { value: "1", label: "Seen in the last hour" },
  { value: "6", label: "Seen in the last 6 hours" },
  { value: "24", label: "Seen in the last 24 hours" },
];
const LANGUAGE_OPTIONS = [
  { value: "", label: "All languages" },
  ...["en", "es", "pt", "de", "fr", "ru", "ja", "ko", "zh", "it", "pl", "tr"].map((l) => ({ value: l, label: l })),
];
const ALLOWED = {
  state: new Set(STATE_OPTIONS.map((o) => o.value)),
  max_rank: new Set(RANK_OPTIONS.map((o) => o.value)),
  hours: new Set(PERIOD_OPTIONS.map((o) => o.value)),
  language: new Set(LANGUAGE_OPTIONS.map((o) => o.value)),
};

function only(value: string | undefined, allowed: Set<string>): string | undefined {
  return value && allowed.has(value) ? value : undefined;
}

export default async function TwitchPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, ["name", "state", "max_rank", "hours", "language"]);
  const filters = {
    name: p.name?.slice(0, 80),
    state: only(p.state, ALLOWED.state),
    max_rank: only(p.max_rank, ALLOWED.max_rank),
    hours: only(p.hours, ALLOWED.hours),
  };
  const language = only(p.language, ALLOWED.language);
  const [summary, games, trending, opportunities, streams] = await Promise.all([
    apiGet<TwitchSummary>("/twitch/summary"),
    apiGet<TwitchGames>("/twitch/games", { ...filters, limit: 50 }),
    apiGet<TwitchGames>("/twitch/trending", { limit: 10 }),
    apiGet<{ items: TwitchOpportunity[]; demo: boolean }>("/twitch/opportunities", { limit: 10 }),
    apiGet<TwitchStreams>("/twitch/streams", { language, limit: 25 }),
  ]);
  return (
    <>
      <PageHeader title="Twitch gaming intelligence">
        Top games, live streams and attention changes from the Twitch Helix API. This is livestream activity on another
        platform: observed viewers are concurrent viewers of the streams returned, never YouTube search volume or demand.
      </PageHeader>
      {!summary.ok ? (
        <ErrorState {...summary} what="Twitch summary" />
      ) : (
        <>
          {summary.data.demo ? (
            <Notice tone="synthetic" title="Demo mode">
              {summary.data.detail ?? "Twitch data is not part of the synthetic dataset."}
            </Notice>
          ) : null}
          <Card title="Provider status and data freshness" className="mb-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={summary.data.provider.status === "available" ? "good" : "warning"}>{humanize(summary.data.provider.status)}</Badge>
              {summary.data.provider.credentials ? <span>credentials: {summary.data.provider.credentials}</span> : null}
              {summary.data.provider.scheduled_discovery === false ? (
                <span className="text-ink-2">scheduled polling off (TREND_AGENT_SCHEDULED_DISCOVERY)</span>
              ) : null}
            </div>
            {summary.data.provider.detail ? <p className="mt-2 text-sm text-ink-2">{summary.data.provider.detail}</p> : null}
            <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-3">
              {Object.entries(summary.data.freshness ?? {}).map(([kind, f]) => (
                <li key={kind}>
                  <span className="font-medium">{humanize(kind)}</span>: {date(f.last_observed_at)}
                  {f.age_minutes !== null ? <span className="text-ink-2"> ({Math.round(f.age_minutes)} min ago)</span> : null}
                </li>
              ))}
            </ul>
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatTile label="Games and categories" value={summary.data.games} hint={`${summary.data.games_real ?? 0} verified games (IGDB)`} />
              <StatTile label="Rising attention" value={summary.data.rising_games ?? 0} hint="between comparable observations" />
              <StatTile label="Monitored games" value={summary.data.monitored_games ?? 0} />
              <StatTile label="Ranking observations" value={summary.data.rank_observations ?? 0} hint="positions, not viewers" />
            </dl>
            {summary.data.retention ? (
              <p className="mt-3 text-xs text-muted">
                Retention {summary.data.retention.hours} h: {summary.data.retention.policy}.
              </p>
            ) : null}
          </Card>
        </>
      )}
      <TwitchEvidenceLegend />
      <FilterForm
        action="/twitch"
        values={p}
        fields={[
          { kind: "text", name: "name", label: "Game", placeholder: "e.g. Fortnite" },
          { kind: "select", name: "max_rank", label: "Ranking", options: RANK_OPTIONS },
          { kind: "select", name: "hours", label: "Observation period", options: PERIOD_OPTIONS },
          { kind: "select", name: "state", label: "Attention status", options: STATE_OPTIONS },
          { kind: "select", name: "language", label: "Stream language", options: LANGUAGE_OPTIONS },
        ]}
      />
      <Card
        title="Top games"
        description={
          games.ok && games.data.snapshot?.observed_at
            ? `Ranking poll ${date(games.data.snapshot.observed_at)}; change measured against the previous poll${games.data.snapshot.previous_observed_at ? ` (${date(games.data.snapshot.previous_observed_at)})` : " (none yet)"}.`
            : NOT_SEARCH_DEMAND
        }
        className="mb-4"
      >
        {games.ok ? <GameTable items={games.data.items} caption="Twitch top games" /> : <ErrorState {...games} what="top games" />}
      </Card>
      <div className="mb-4 grid gap-4 xl:grid-cols-2">
        <Card title="Attention momentum" description="Rank or observed-viewer increases between comparable stored observations.">
          {trending.ok ? (
            <GameTable items={trending.data.items} caption="Games with rising Twitch attention" empty="No game has risen between comparable observations yet; nothing is estimated." />
          ) : (
            <ErrorState {...trending} what="attention momentum" />
          )}
        </Card>
        <Card title="Emerging gaming opportunities" description="Proxy opportunities: hypotheses linked to Phase 5 gaps, Phase 7 recommendations and Phase 6 forecasts where they exist.">
          {opportunities.ok ? <OpportunityList items={opportunities.data.items} /> : <ErrorState {...opportunities} what="opportunities" />}
        </Card>
      </div>
      <Card
        title="Active streams"
        description={
          streams.ok && streams.data.snapshot
            ? `${humanize(streams.data.snapshot.kind)} collected ${date(streams.data.snapshot.observed_at)}: ${streams.data.snapshot.streams ?? 0} streams, ${humanize(streams.data.snapshot.sample_status)} sample (${streams.data.snapshot.status_reason.replaceAll("_", " ")}).`
            : "No stream collection stored yet."
        }
      >
        {streams.ok ? <StreamTable items={streams.data.items} /> : <ErrorState {...streams} what="streams" />}
      </Card>
    </>
  );
}
