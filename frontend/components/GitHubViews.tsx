import Link from "next/link";

import { SyntheticBadge } from "@/components/Badges";
import { InfoTip } from "@/components/ui/InfoTip";
import { Badge, EmptyState, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { date, humanize, num, safeUrl } from "@/lib/format";
import { EVIDENCE_CLASS, EVIDENCE_ORDER, type EvidenceClass, type GitHubRelease, type GitHubRepo, type Momentum } from "@/lib/github";

export function EvidenceClassBadge({ value }: { value: EvidenceClass }) {
  const info = EVIDENCE_CLASS[value];
  return (
    <InfoTip content={info.description}>
      <Badge tone={info.tone}>{info.label}</Badge>
    </InfoTip>
  );
}

export function EvidenceLegend() {
  return (
    <section aria-labelledby="gh-legend" className="mb-4 rounded-xl border border-line bg-surface p-4">
      <h2 id="gh-legend" className="text-sm font-semibold text-ink">
        Evidence classes
      </h2>
      <ul className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
        {EVIDENCE_ORDER.map((k) => (
          <li key={k} className="flex flex-col gap-1">
            <span>
              <EvidenceClassBadge value={k} />
            </span>
            <span className="text-ink-2">{EVIDENCE_CLASS[k].description}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  const url = safeUrl(href);
  if (!url) return <>{children}</>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
      {children}
    </a>
  );
}

export function RepositoryTable({ items, caption }: { items: GitHubRepo[]; caption: string }) {
  if (items.length === 0) {
    return <EmptyState title="Nothing discovered yet">Run `trend-agent github discover` or the scheduler.</EmptyState>;
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Repository</th>
            <th scope="col" className={thClass}>Created</th>
            <th scope="col" className={thClass}>
              <InfoTip content="Cumulative count at the latest observation. Not recent growth, not unique people, not demand.">
                <span>Stars (total)</span>
              </InfoTip>
            </th>
            <th scope="col" className={thClass}>Discovered</th>
            <th scope="col" className={thClass}>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.repository_id}>
              <td className={tdClass}>
                <Link href={`/github/${r.repository_id}`} className="font-medium text-accent hover:underline">
                  {r.full_name}
                </Link>
                {r.description ? <div className="mt-1 max-w-xl text-sm text-ink-2">{r.description}</div> : null}
                <div className="mt-1 flex flex-wrap gap-1 text-xs text-ink-2">
                  {r.language ? <Badge>{r.language}</Badge> : null}
                  {r.topics.slice(0, 5).map((t) => (
                    <Badge key={t}>{t}</Badge>
                  ))}
                  {r.quality_flags.map((f) => (
                    <Badge key={f} tone="warning">
                      {humanize(f)}
                    </Badge>
                  ))}
                  {r.synthetic ? <SyntheticBadge /> : null}
                </div>
                <div className="mt-1 text-xs">
                  <ExternalLink href={r.html_url}>Source on GitHub</ExternalLink>
                </div>
              </td>
              <td className={tdClass}>{date(r.created_at)}</td>
              <td className={`${tdClass} tabular`}>{r.stargazers_count.toLocaleString("en-GB")}</td>
              <td className={tdClass}>
                {date(r.first_seen_at)}
                <div className="text-xs text-ink-2">via {r.strategies.join(", ")}</div>
              </td>
              <td className={tdClass}>
                <EvidenceClassBadge value={r.evidence_class} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ReleaseTable({ items }: { items: GitHubRelease[] }) {
  if (items.length === 0) {
    return <EmptyState title="No releases stored yet">Monitored repositories are polled on their configured interval.</EmptyState>;
  }
  return (
    <div className="overflow-x-auto">
      <table className={tableClass}>
        <caption className="sr-only">Recent software releases</caption>
        <thead>
          <tr>
            <th scope="col" className={thClass}>Release</th>
            <th scope="col" className={thClass}>Level</th>
            <th scope="col" className={thClass}>Published</th>
            <th scope="col" className={thClass}>From the release notes (quoted)</th>
            <th scope="col" className={thClass}>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.release_id}>
              <td className={tdClass}>
                <ExternalLink href={r.html_url}>
                  {r.repository} {r.tag_name}
                </ExternalLink>
                {r.version ? <div className="text-xs text-ink-2">version {r.version}</div> : null}
              </td>
              <td className={tdClass}>
                {humanize(r.release_level)}
                {r.is_breaking ? (
                  <div>
                    <Badge tone="critical">Breaking changes noted</Badge>
                  </div>
                ) : null}
              </td>
              <td className={tdClass}>{date(r.published_at)}</td>
              <td className={tdClass}>
                {[...r.breaking_change_lines, ...r.new_capability_lines].slice(0, 4).map((line, i) => (
                  <blockquote key={i} className="mb-1 border-l-2 border-line pl-2 text-sm text-ink-2">
                    {line}
                  </blockquote>
                ))}
                {r.breaking_change_lines.length + r.new_capability_lines.length === 0 ? (
                  <span className="text-sm text-muted">no breaking-change or feature lines found</span>
                ) : null}
              </td>
              <td className={tdClass}>
                <EvidenceClassBadge value={r.evidence_class} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MomentumPanel({ m }: { m: Momentum }) {
  return (
    <div>
      <p className="text-sm text-ink-2">
        {m.signal_label}: {humanize(m.status)} from {m.observations} observation{m.observations === 1 ? "" : "s"}
        {m.interval_hours !== null ? ` over ${num(m.interval_hours)} h` : ""}.
      </p>
      <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-ink-2">New stars between observations</dt>
          <dd className="tabular">{m.star_delta === null ? "not measured" : m.star_delta}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Stars per day</dt>
          <dd className="tabular">{m.stars_per_day === null ? "not measured" : num(m.stars_per_day)}</dd>
        </div>
        <div>
          <dt className="text-ink-2">New forks</dt>
          <dd className="tabular">{m.fork_delta === null ? "not measured" : m.fork_delta}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Repository age</dt>
          <dd className="tabular">{m.repository_age_days === null ? "unknown" : `${num(m.repository_age_days, 0)} days`}</dd>
        </div>
      </dl>
      <ul className="mt-2 list-disc pl-5 text-xs text-muted">
        {[...m.notes, ...m.caveats].map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
    </div>
  );
}
