import Link from "next/link";

import { SyntheticBadge } from "@/components/Badges";
import { InfoTip } from "@/components/ui/InfoTip";
import { Badge, EmptyState, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { date, humanize, num, safeUrl } from "@/lib/format";
import {
  HN_EVIDENCE_CLASS,
  HN_EVIDENCE_ORDER,
  KIND_LABEL,
  type HNEvidenceClass,
  type HNMomentum,
  type HNObservation,
  type HNSourceHealth,
  type HNStory,
  type PriorityComponent,
} from "@/lib/hackernews";
import { CATEGORY_LABEL } from "@/lib/labels";

export function HNEvidenceBadge({ value }: { value: HNEvidenceClass }) {
  const info = HN_EVIDENCE_CLASS[value];
  return (
    <InfoTip content={info.description}>
      <Badge tone={info.tone}>{info.label}</Badge>
    </InfoTip>
  );
}

export function HNEvidenceLegend() {
  return (
    <section aria-labelledby="hn-legend" className="mb-4 rounded-xl border border-line bg-surface p-4">
      <h2 id="hn-legend" className="text-sm font-semibold text-ink">
        Evidence classes
      </h2>
      <ul className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
        {HN_EVIDENCE_ORDER.map((k) => (
          <li key={k} className="flex flex-col gap-1">
            <span>
              <HNEvidenceBadge value={k} />
            </span>
            <span className="text-ink-2">{HN_EVIDENCE_CLASS[k].description}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ExternalLink({ href, children }: { href: string | null; children: React.ReactNode }) {
  const url = safeUrl(href);
  if (!url) return <>{children}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
      {children}
    </a>
  );
}

function category(value: string): string {
  return CATEGORY_LABEL[value] ?? humanize(value);
}

export function CategoryBadges({ s }: { s: Pick<HNStory, "primary_category" | "secondary_category" | "ambiguous"> }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      <Badge>{category(s.primary_category)}</Badge>
      {s.secondary_category ? <Badge>also {category(s.secondary_category)}</Badge> : null}
      {s.ambiguous ? (
        <InfoTip content="Two categories scored almost equally; both are kept rather than forcing one.">
          <Badge tone="warning">Ambiguous</Badge>
        </InfoTip>
      ) : null}
    </span>
  );
}

function rate(v: number | null): string {
  return v === null ? "not measured" : `${v > 0 ? "+" : ""}${num(v)}/h`;
}

export function StoryTable({ items, caption, empty }: { items: HNStory[]; caption: string; empty?: string }) {
  if (items.length === 0) {
    return (
      <EmptyState title="No stories">
        {empty ?? "Nothing stored for this view yet. Run `trend-agent hackernews discover` or the scheduler."}
      </EmptyState>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Story</th>
            <th scope="col" className={thClass}>Published</th>
            <th scope="col" className={thClass}>
              <InfoTip content="Points and comments at the latest observation: cumulative community actions, not views, not people, not demand.">
                <span>Points / comments (total)</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>
              <InfoTip content="Points per hour between two stored observations at least the minimum interval apart. 'Not measured' until then; never estimated.">
                <span>Discussion momentum</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>
              <InfoTip content="Heuristic ranking (relevance, novelty, recency, activity, launch significance, corroboration, entity verification, list reliability). Decides what to look at; it is not demand.">
                <span>Priority</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {items.map((s) => (
            <tr key={s.story_id}>
              <td className={tdClass}>
                <Link href={`/hackernews/${s.story_id}`} className="font-medium text-accent hover:underline">
                  {s.title}
                </Link>
                <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-2">
                  <Badge tone={s.kind === "story" ? "neutral" : "info"}>{KIND_LABEL[s.kind] ?? humanize(s.kind)}</Badge>
                  <CategoryBadges s={s} />
                  {s.lists.map((l) => (
                    <Badge key={l}>{l}</Badge>
                  ))}
                  {s.synthetic ? <SyntheticBadge /> : null}
                </div>
                <div className="mt-1 flex flex-wrap gap-3 text-xs">
                  <ExternalLink href={s.discussion_url}>Discussion</ExternalLink>
                  {s.linked_url ? <ExternalLink href={s.linked_url}>{s.domain ?? "Linked article"}</ExternalLink> : null}
                </div>
              </td>
              <td className={tdClass}>
                {date(s.published_at)}
                <div className="text-xs text-ink-2">{num(s.age_hours, 0)} h ago</div>
              </td>
              <td className={`${tdClass} tabular`}>
                {s.score.toLocaleString("en-GB")} / {s.comments.toLocaleString("en-GB")}
              </td>
              <td className={tdClass}>
                <span className="tabular">{rate(s.score_per_hour)}</span>
                {s.momentum_state === "rising" ? (
                  <div>
                    <Badge tone="good">Rising discussion</Badge>
                  </div>
                ) : null}
              </td>
              <td className={`${tdClass} tabular`}>{num(s.priority_score, 0)}</td>
              <td className={tdClass}>
                <HNEvidenceBadge value={s.evidence_class} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function HNMomentumPanel({ m }: { m: HNMomentum }) {
  const observations = m.observations ?? 0;
  return (
    <div>
      <p className="text-sm text-ink-2">
        {m.signal_label ?? "community discussion activity"}: {humanize(m.status ?? "not computed")} from {observations}{" "}
        observation{observations === 1 ? "" : "s"}
        {m.interval_hours !== null && m.interval_hours !== undefined ? ` over ${num(m.interval_hours, 2)} h` : ""}.
      </p>
      <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-ink-2">Point change</dt>
          <dd className="tabular">{m.score_delta === null || m.score_delta === undefined ? "not measured" : m.score_delta}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Points per hour</dt>
          <dd className="tabular">{rate(m.score_per_hour ?? null)}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Comment change</dt>
          <dd className="tabular">
            {m.comments_delta === null || m.comments_delta === undefined ? "not measured" : m.comments_delta}
          </dd>
        </div>
        <div>
          <dt className="text-ink-2">Comments per hour</dt>
          <dd className="tabular">{rate(m.comments_per_hour ?? null)}</dd>
        </div>
      </dl>
      <ul className="mt-2 list-disc pl-5 text-xs text-muted">
        {[...(m.notes ?? []), ...(m.caveats ?? [])].map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
    </div>
  );
}

export function PriorityBreakdown({ items }: { items: [string, PriorityComponent][] }) {
  if (items.length === 0) return <EmptyState title="Not scored yet" />;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Priority components</caption>
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
          {items.map(([name, c]) => (
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

export function ObservationTable({ items }: { items: HNObservation[] }) {
  if (items.length === 0) return <EmptyState title="No observations stored" />;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Stored observations of this story</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Collected</th>
            <th scope="col" className={thClass}>Points</th>
            <th scope="col" className={thClass}>Comments</th>
            <th scope="col" className={thClass}>How</th>
          </tr>
        </thead>
        <tbody>
          {items.map((o) => (
            <tr key={o.observed_at}>
              <td className={tdClass}>{date(o.observed_at)}</td>
              <td className={`${tdClass} tabular`}>{o.score ?? "—"}</td>
              <td className={`${tdClass} tabular`}>{o.comments ?? "—"}</td>
              <td className={`${tdClass} text-ink-2`}>
                {o.origin === "refresh" ? "refresh" : `${o.list ?? "list"} list${o.rank ? `, rank ${o.rank}` : ""}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SourceHealthTable({ items }: { items: HNSourceHealth[] }) {
  if (items.length === 0) return <EmptyState title="No sources polled yet" />;
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Hacker News list sources</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>List</th>
            <th scope="col" className={thClass}>Health</th>
            <th scope="col" className={thClass}>Polled every</th>
            <th scope="col" className={thClass}>Last success</th>
            <th scope="col" className={thClass}>Failures</th>
          </tr>
        </thead>
        <tbody>
          {items.map((h) => (
            <tr key={h.source_key}>
              <td className={tdClass}>{h.list}</td>
              <td className={tdClass}>
                <Badge tone={h.health === "healthy" ? "good" : "warning"}>{humanize(h.health)}</Badge>
                {h.last_error ? <div className="mt-1 text-xs text-ink-2">{h.last_error}</div> : null}
              </td>
              <td className={tdClass}>{h.poll_interval_minutes} min</td>
              <td className={tdClass}>{date(h.last_success_at)}</td>
              <td className={`${tdClass} tabular`}>{h.consecutive_failures}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
