import Link from "next/link";

import { RecStatusBadge, SyntheticBadge } from "@/components/Badges";
import { InfoTip } from "@/components/ui/InfoTip";
import { Badge, EmptyState, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { age, date, hours, humanize, pct, score } from "@/lib/format";
import { CATEGORY_LABEL, URGENCY } from "@/lib/labels";
import type { FreshnessView, OpportunitySummary, ProviderView, RecommendationSummary } from "@/lib/types";

export function RecommendationTable({
  items,
  caption,
  showRank = true,
}: {
  items: RecommendationSummary[];
  caption: string;
  showRank?: boolean;
}) {
  if (!items.length) {
    return <EmptyState title="No recommendations to show">Adjust the filters or generate ideas.</EmptyState>;
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {showRank ? <th scope="col" className={thClass}>Rank</th> : null}
            <th scope="col" className={thClass}>Idea</th>
            <th scope="col" className={thClass}>
              <InfoTip content="A 0-100 research-priority index from versioned, experimental weights. Not a probability of success.">
                <span className="underline decoration-dotted">Score</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Status</th>
            <th scope="col" className={thClass}>Effort</th>
            <th scope="col" className={thClass}>Urgency</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id}>
              {showRank ? <td className={`${tdClass} tabular`}>{r.rank ?? "—"}</td> : null}
              <td className={tdClass}>
                <Link href={`/recommendations/${r.id}`} className="font-medium text-accent hover:underline">
                  {r.title}
                </Link>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-2">
                  <span>{r.format_label}</span>
                  <span aria-hidden="true">·</span>
                  <span>{CATEGORY_LABEL[r.category ?? ""] ?? humanize(r.category)}</span>
                  {r.synthetic ? <SyntheticBadge /> : null}
                  {r.saved ? <Badge tone="good">Saved</Badge> : null}
                  {r.production_status ? <Badge>{humanize(r.production_status)}</Badge> : null}
                  {r.suppressed ? <Badge tone="neutral">Suppressed: {r.suppression_reason}</Badge> : null}
                </div>
              </td>
              <td className={`${tdClass} tabular`}>{score(r.score)}</td>
              <td className={tdClass}>
                <RecStatusBadge value={r.status} />
              </td>
              <td className={`${tdClass} whitespace-nowrap`}>
                {hours(r.hours_low, r.hours_high)}
                <div className="text-xs text-ink-2">{humanize(r.production_profile)}</div>
              </td>
              <td className={tdClass}>{URGENCY[r.urgency] ?? humanize(r.urgency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OpportunityTable({ items }: { items: OpportunitySummary[] }) {
  if (!items.length) {
    return <EmptyState title="No opportunities match">Change or clear the filters.</EmptyState>;
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Content opportunities</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Query</th>
            <th scope="col" className={thClass}>Demand evidence</th>
            <th scope="col" className={thClass}>Gap</th>
            <th scope="col" className={thClass}>
              <InfoTip content="Phase 5 opportunity score: a research-priority index, not a probability.">
                <span className="underline decoration-dotted">Score</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Forecast</th>
            <th scope="col" className={thClass}>Ideas</th>
            <th scope="col" className={thClass}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((o) => (
            <tr key={o.id}>
              <td className={tdClass}>
                <Link href={`/opportunities/${o.id}`} className="font-medium text-accent hover:underline">
                  {o.query}
                </Link>
                <div className="mt-1 flex flex-wrap gap-2 text-xs text-ink-2">
                  <span>{CATEGORY_LABEL[o.category ?? ""] ?? humanize(o.category)}</span>
                  <span>{humanize(o.intent)}</span>
                  <Badge>{humanize(o.status)}</Badge>
                  {o.synthetic ? <SyntheticBadge /> : null}
                </div>
              </td>
              <td className={tdClass}>
                <span className={o.direct_youtube_search ? "text-ink" : "text-ink-2"}>{o.demand_label}</span>
                {o.surge_stage ? <div className="text-xs text-ink-2">Surge stage: {humanize(o.surge_stage)}</div> : null}
              </td>
              <td className={tdClass}>
                {o.gap_type ? (
                  <>
                    {humanize(o.gap_type)}
                    <div className="text-xs text-ink-2">{humanize(o.gap_strength)} evidence</div>
                  </>
                ) : (
                  <span className="text-ink-2">None verified</span>
                )}
              </td>
              <td className={`${tdClass} tabular`}>{score(o.opportunity_score)}</td>
              <td className={tdClass}>
                {o.breakout_probability !== null ? (
                  <>
                    <span className="tabular">{pct(o.breakout_probability)}</span> breakout
                    <div className="text-xs text-ink-2">{humanize(o.breakout_calibration)}</div>
                  </>
                ) : (
                  <span className="text-ink-2">{o.forecast_status ? humanize(o.forecast_status) : "No forecast"}</span>
                )}
              </td>
              <td className={`${tdClass} tabular`}>
                {o.approved_count}/{o.recommendation_count}
              </td>
              <td className={`${tdClass} whitespace-nowrap text-xs`}>{date(o.last_updated)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const PROVIDER_TONE: Record<string, "good" | "warning" | "critical" | "neutral" | "synthetic"> = {
  ok: "good",
  degraded: "warning",
  unavailable: "critical",
  unconfigured: "neutral",
  unknown: "neutral",
  synthetic: "synthetic",
};

export function ProviderList({ items }: { items: ProviderView[] }) {
  if (!items.length) return <p className="text-sm text-ink-2">No providers reported.</p>;
  return (
    <ul className="divide-y divide-line">
      {items.map((p) => (
        <li key={p.name} className="flex flex-wrap items-start justify-between gap-2 py-2">
          <div>
            <p className="text-sm font-medium text-ink">{humanize(p.name)}</p>
            <p className="text-xs text-ink-2">{p.detail}</p>
          </div>
          <Badge tone={PROVIDER_TONE[p.status] ?? "neutral"}>{humanize(p.status)}</Badge>
        </li>
      ))}
    </ul>
  );
}

export function FreshnessTable({ items }: { items: FreshnessView[] }) {
  if (!items.length) return <p className="text-sm text-ink-2">No time series are stored yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Data freshness per provider and measurement</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Provider</th>
            <th scope="col" className={thClass}>Measurement</th>
            <th scope="col" className={thClass}>Series</th>
            <th scope="col" className={thClass}>Latest value</th>
            <th scope="col" className={thClass}>State</th>
          </tr>
        </thead>
        <tbody>
          {items.map((f, i) => (
            <tr key={`${f.provider}-${f.measurement}-${i}`}>
              <td className={tdClass}>{f.provider}</td>
              <td className={tdClass}>{humanize(f.measurement)}</td>
              <td className={`${tdClass} tabular`}>{f.series}</td>
              <td className={tdClass}>{f.latest_observation ? `${age(f.age_hours)} ago` : "none"}</td>
              <td className={tdClass}>
                <Badge
                  tone={
                    f.status === "ok" ? "good" : f.status === "stale" ? "warning" : f.status === "synthetic" ? "synthetic" : "neutral"
                  }
                >
                  {humanize(f.status)}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
