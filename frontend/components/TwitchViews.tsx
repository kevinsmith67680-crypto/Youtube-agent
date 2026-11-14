import Link from "next/link";

import { InfoTip } from "@/components/ui/InfoTip";
import { Badge, EmptyState, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { date, humanize, num, safeUrl } from "@/lib/format";
import {
  STATE_LABEL,
  TWITCH_EVIDENCE,
  TWITCH_EVIDENCE_ORDER,
  type AttentionComponent,
  type FormatCandidate,
  type TwitchEvidenceClass,
  type TwitchGame,
  type TwitchGameDetail,
  type TwitchOpportunity,
  type TwitchStream,
} from "@/lib/twitch";

export function TwitchEvidenceBadge({ value }: { value: TwitchEvidenceClass }) {
  const info = TWITCH_EVIDENCE[value];
  return (
    <InfoTip content={info.description}>
      <Badge tone={info.tone}>{info.label}</Badge>
    </InfoTip>
  );
}

export function TwitchEvidenceLegend() {
  return (
    <section aria-labelledby="tw-legend" className="mb-4 rounded-xl border border-line bg-surface p-4">
      <h2 id="tw-legend" className="text-sm font-semibold text-ink">
        Evidence classes
      </h2>
      <ul className="mt-2 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
        {TWITCH_EVIDENCE_ORDER.map((k) => (
          <li key={k} className="flex flex-col gap-1">
            <span>
              <TwitchEvidenceBadge value={k} />
            </span>
            <span className="text-ink-2">{TWITCH_EVIDENCE[k].description}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function intText(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toLocaleString("en-GB");
}

function signed(v: number | null | undefined, suffix = ""): string {
  if (v === null || v === undefined) return "not measured";
  return `${v > 0 ? "+" : ""}${num(v)}${suffix}`;
}

export function AttentionBadge({ state }: { state: string }) {
  const tone = state === "rising" || state === "sustained_rise" ? "good" : state === "falling" ? "warning" : "neutral";
  return <Badge tone={tone}>{STATE_LABEL[state] ?? humanize(state)}</Badge>;
}

export function SampleBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-muted">no sample</span>;
  if (status === "partial") return <TwitchEvidenceBadge value="PARTIAL_SAMPLE" />;
  return <Badge tone={status === "complete" ? "good" : "neutral"}>{humanize(status)} sample</Badge>;
}

export function GameTable({ items, caption, empty }: { items: TwitchGame[]; caption: string; empty?: string }) {
  if (items.length === 0) {
    return <EmptyState title="No games">{empty ?? "Nothing stored yet. Run `trend-agent twitch cycle` or the scheduler."}</EmptyState>;
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>
              <InfoTip content="Position in Get Top Games at the latest poll. The endpoint returns positions only, never viewer numbers.">
                <span>Rank</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Game</th>
            <th scope="col" className={thClass}>
              <InfoTip content="Positions gained since the previous stored ranking poll (negative = fell).">
                <span>Rank change</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>
              <InfoTip content="Sum of concurrent viewers over the streams returned for this game. Only a game total when the sample is complete.">
                <span>Observed viewers</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Attention</th>
            <th scope="col" className={thClass}>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {items.map((g) => (
            <tr key={g.game_id}>
              <td className={`${tdClass} tabular`}>{g.in_latest_ranking === false ? "outside top-N" : intText(g.latest_rank)}</td>
              <td className={tdClass}>
                <Link href={`/twitch/${g.game_id}`} className="font-medium text-accent hover:underline">
                  {g.name}
                </Link>
                <div className="mt-1 flex flex-wrap gap-1 text-xs text-ink-2">
                  {g.category_kind !== "game" ? <Badge>{g.category_kind === "non_game" ? "Not a game" : "Category unverified"}</Badge> : null}
                  {g.new_in_top ? <Badge tone="good">New in top-N</Badge> : null}
                  {g.promoted_at ? <Badge tone="info">Sent to discovery</Badge> : null}
                </div>
              </td>
              <td className={`${tdClass} tabular`}>{signed(g.rank_change_since_previous_poll ?? null)}</td>
              <td className={tdClass}>
                <span className="tabular">{intText(g.observed_viewers)}</span>
                {g.observed_viewers !== null ? (
                  <div className="mt-1 text-xs">
                    <SampleBadge status={g.sample_status} />
                  </div>
                ) : null}
              </td>
              <td className={tdClass}>
                <AttentionBadge state={g.attention_state} />
                <div className="text-xs text-ink-2 tabular">score {num(g.attention_score, 0)}</div>
              </td>
              <td className={tdClass}>
                <div className="flex flex-wrap gap-1">
                  {g.labels
                    .filter((l): l is TwitchEvidenceClass => l in TWITCH_EVIDENCE)
                    .map((l) => (
                      <TwitchEvidenceBadge key={l} value={l} />
                    ))}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StreamTable({ items }: { items: TwitchStream[] }) {
  if (items.length === 0) return <EmptyState title="No streams stored">Live streams are polled every 10 minutes by the scheduler.</EmptyState>;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Live streams from the latest collection</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>#</th>
            <th scope="col" className={thClass}>Channel</th>
            <th scope="col" className={thClass}>Game</th>
            <th scope="col" className={thClass}>Language</th>
            <th scope="col" className={thClass}>
              <InfoTip content="Concurrent viewers of this stream at collection time.">
                <span>Viewers</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Live since</th>
          </tr>
        </thead>
        <tbody>
          {items.map((s) => {
            const url = safeUrl(s.channel_url);
            return (
              <tr key={s.stream_id}>
                <td className={`${tdClass} tabular`}>{s.position}</td>
                <td className={tdClass}>
                  {url ? (
                    <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
                      {s.user_name ?? s.user_login}
                    </a>
                  ) : (
                    s.user_login
                  )}
                  {s.title ? <div className="mt-1 max-w-md text-xs text-ink-2">{s.title}</div> : null}
                </td>
                <td className={tdClass}>
                  {s.game_id ? (
                    <Link href={`/twitch/${s.game_id}`} className="text-accent hover:underline">
                      {s.game_name ?? s.game_id}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td className={tdClass}>{s.language ?? "—"}</td>
                <td className={`${tdClass} tabular`}>{intText(s.viewer_count)}</td>
                <td className={tdClass}>{date(s.started_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ComponentTable({ components }: { components: Record<string, AttentionComponent> | undefined }) {
  const rows = Object.entries(components ?? {});
  if (rows.length === 0) return <EmptyState title="Not assessed yet" />;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Attention score components</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Component</th>
            <th scope="col" className={thClass}>Value (0–1)</th>
            <th scope="col" className={thClass}>Weight</th>
            <th scope="col" className={thClass}>Points</th>
            <th scope="col" className={thClass}>Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, c]) => (
            <tr key={name}>
              <th scope="row" className={`${tdClass} font-medium`}>
                {humanize(name)}
              </th>
              <td className={`${tdClass} tabular`}>{num(c.value, 2)}</td>
              <td className={`${tdClass} tabular`}>{num(c.weight, 2)}</td>
              <td className={`${tdClass} tabular`}>{num(c.contribution, 1)}</td>
              <td className={`${tdClass} text-ink-2`}>{c.basis}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RankHistory({ d }: { d: TwitchGameDetail["history"] }) {
  if (d.ranks.length === 0) {
    return <EmptyState title="No ranking observations">The game was not in the polled top-N during the window.</EmptyState>;
  }
  return (
    <>
      <div className="overflow-x-auto">
        <table className={tableClass}>
          <caption className="sr-only">Stored ranking observations</caption>
          <thead>
            <tr>
              <th scope="col" className={thClass}>Collected</th>
              <th scope="col" className={thClass}>Rank</th>
            </tr>
          </thead>
          <tbody>
            {d.ranks.map((r) => (
              <tr key={r.observed_at}>
                <td className={tdClass}>{date(r.observed_at)}</td>
                <td className={`${tdClass} tabular`}>{r.rank}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-muted">
        {d.top_games_polls} ranking polls in the window; outside the polled top-N in {d.polls_outside_top_n}. {d.note}
      </p>
    </>
  );
}

export function ViewerHistory({ d }: { d: TwitchGameDetail["history"] }) {
  if (d.viewers.length === 0) return <EmptyState title="No viewer observations">Monitor the game to collect its own stream list.</EmptyState>;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Stored observed-viewer samples</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Collected</th>
            <th scope="col" className={thClass}>Observed viewers</th>
            <th scope="col" className={thClass}>Streams</th>
            <th scope="col" className={thClass}>Sample</th>
            <th scope="col" className={thClass}>Basis</th>
          </tr>
        </thead>
        <tbody>
          {d.viewers.map((v) => (
            <tr key={`${v.observed_at}-${v.sample_basis}`}>
              <td className={tdClass}>{date(v.observed_at)}</td>
              <td className={`${tdClass} tabular`}>{intText(v.viewers_observed)}</td>
              <td className={`${tdClass} tabular`}>{intText(v.streams_observed)}</td>
              <td className={tdClass}>
                <SampleBadge status={v.sample_status} />
              </td>
              <td className={`${tdClass} text-ink-2`}>
                {v.sample_basis === "game_streams" ? "game's own stream list" : "global top streams"} (cap {v.sample_cap})
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function FormatList({ formats }: { formats: FormatCandidate[] }) {
  if (formats.length === 0) return <p className="text-sm text-ink-2">No grounded formats yet.</p>;
  return (
    <ul className="flex flex-col gap-2 text-sm">
      {formats.map((f) => (
        <li key={f.format} className="flex flex-wrap items-center gap-2">
          <Badge tone={f.eligible ? "good" : "neutral"}>{humanize(f.format)}</Badge>
          <span className="text-ink-2">
            {f.eligible ? `grounded by ${f.grounded_by.join(", ")}` : `needs ${f.requires}`}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function OpportunityList({ items }: { items: TwitchOpportunity[] }) {
  if (items.length === 0) {
    return (
      <EmptyState title="No emerging games yet">
        Games appear here when their Twitch attention rises between comparable observations and they resolve to a verified game.
      </EmptyState>
    );
  }
  return (
    <ul className="flex flex-col gap-4">
      {items.map((o) => (
        <li key={o.game_id} className="rounded-lg border border-line p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/twitch/${o.game_id}`} className="font-medium text-accent hover:underline">
              {o.name}
            </Link>
            <TwitchEvidenceBadge value="PROXY_OPPORTUNITY" />
            <AttentionBadge state={o.attention_state} />
            {o.entity ? (
              <Badge tone={o.entity.status === "verified" ? "good" : "warning"}>
                entity {humanize(o.entity.status)}
                {o.entity.evergreen ? ", evergreen" : ""}
              </Badge>
            ) : (
              <Badge>no entity yet</Badge>
            )}
          </div>
          {o.keyword_hypotheses.length ? (
            <p className="mt-2 text-sm">
              Hypotheses to test: {o.keyword_hypotheses.slice(0, 5).map((k) => k.query).join(" · ")}
            </p>
          ) : (
            <p className="mt-2 text-sm text-ink-2">No keyword hypotheses (entity not verified, evergreen, or not yet promoted).</p>
          )}
          <p className="mt-1 text-xs text-ink-2">
            Phase 5 gaps {o.content_gaps.length} · opportunities {o.content_opportunities.length} · Phase 7 recommendations{" "}
            {o.recommendations.length} · Phase 6 forecasts {o.forecasts.length}
          </p>
        </li>
      ))}
    </ul>
  );
}
