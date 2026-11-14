import type { Metadata } from "next";
import Link from "next/link";

import { SyntheticBadge } from "@/components/Badges";
import { ForecastPanel } from "@/components/charts/ForecastPanel";
import { ErrorState } from "@/components/ErrorState";
import { RecommendationTable } from "@/components/Tables";
import { Badge, Card, Notice, PageHeader, tableClass, tdClass, thClass } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize, safeUrl, score } from "@/lib/format";
import type { GapView, OpportunityDetail } from "@/lib/types";

export const metadata: Metadata = { title: "Opportunity" };
export const dynamic = "force-dynamic";

const STRENGTH_TONE = { strong: "good", moderate: "warning", weak: "neutral" } as const;

function Gap({ g, primary }: { g: GapView; primary: boolean }) {
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{humanize(g.gap_type)}</span>
        {primary ? <Badge tone="info">Primary gap</Badge> : null}
        <Badge tone={STRENGTH_TONE[g.evidence_strength as keyof typeof STRENGTH_TONE] ?? "neutral"}>
          {humanize(g.evidence_strength)} evidence
        </Badge>
        <Badge>{humanize(g.verification_status)}</Badge>
      </div>
      <p className="mt-1 text-sm text-ink-2">{g.statement}</p>
      {g.limitations.length ? (
        <ul className="mt-1 list-disc pl-5 text-xs text-muted">
          {g.limitations.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export default async function OpportunityPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const res = await apiGet<OpportunityDetail>(`/opportunities/${encodeURIComponent(id)}`);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Opportunity" />
        <ErrorState {...res} what="opportunity" />
      </>
    );
  }
  const d = res.data;
  const s = d.summary;
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/opportunities" className="text-accent hover:underline">
          Opportunities
        </Link>
      </nav>
      <PageHeader title={s.query}>
        <div className="flex flex-wrap items-center gap-2">
          <Badge>{humanize(s.status)}</Badge>
          <Badge tone="info">Score {score(s.opportunity_score)}</Badge>
          <Badge>{humanize(s.intent)}</Badge>
          {s.synthetic ? <SyntheticBadge /> : null}
          <span className="text-xs text-muted">Analysed {date(s.last_updated)}</span>
        </div>
      </PageHeader>
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Demand evidence" className="lg:col-span-2">
          <p className="text-sm font-medium text-ink">{s.demand_label}</p>
          <p className="mt-1 text-sm text-ink-2">{d.demand_statement}</p>
          {!s.direct_youtube_search ? (
            <div className="mt-3">
              <Notice tone="warning">
                This is not a direct YouTube Search measurement. It is not presented as YouTube
                search demand.
              </Notice>
            </div>
          ) : null}
          {d.demand_limitations.length ? (
            <ul className="mt-2 list-disc pl-5 text-xs text-muted">
              {d.demand_limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          ) : null}
        </Card>
        <Card title="Main uncertainty">
          <p className="text-sm text-ink-2">{d.main_uncertainty || "None recorded."}</p>
          {d.coverage_limitations.length ? (
            <ul className="mt-2 list-disc pl-5 text-xs text-muted">
              {d.coverage_limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          ) : null}
        </Card>
        <Card title="Content gaps" id="gaps" className="lg:col-span-2">
          {d.gaps.length ? (
            <ul className="divide-y divide-line">
              {d.gaps.map((g, i) => (
                <Gap
                  key={`${g.gap_type}-${i}`}
                  g={g}
                  primary={!!d.primary_gap && d.primary_gap.statement === g.statement && d.primary_gap.gap_type === g.gap_type}
                />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-2">No verifiable gap was found.</p>
          )}
        </Card>
        <Card title="Forecast">
          <ForecastPanel forecasts={d.forecasts} observed={null} compact />
        </Card>
        <Card title="Information requirements" className="lg:col-span-3">
          {d.requirements.length ? (
            <div className="overflow-x-auto">
              <table className={tableClass}>
                <caption className="sr-only">Information requirements and how well results cover them</caption>
                <thead>
                  <tr>
                    <th scope="col" className={thClass}>Requirement</th>
                    <th scope="col" className={thClass}>Priority</th>
                    <th scope="col" className={thClass}>Coverage in results</th>
                    <th scope="col" className={thClass}>Basis</th>
                  </tr>
                </thead>
                <tbody>
                  {d.requirements.map((r) => (
                    <tr key={r.key}>
                      <td className={tdClass}>
                        {r.requirement}
                        <div className="text-xs text-ink-2">{r.statement}</div>
                      </td>
                      <td className={tdClass}>{humanize(r.priority)}</td>
                      <td className={tdClass}>{humanize(r.coverage)}</td>
                      <td className={tdClass}>{humanize(r.evidence_tier)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-ink-2">No requirements recorded.</p>
          )}
        </Card>
        <Card
          title="Competing videos inspected"
          className="lg:col-span-3"
          description={`Scope: ${d.supply_scope}. Videos are referenced by ID; findings say whether they rest on metadata or on authorised full content.`}
        >
          {d.competing_videos.length ? (
            <div className="overflow-x-auto">
              <table className={tableClass}>
                <caption className="sr-only">Inspected search results</caption>
                <thead>
                  <tr>
                    <th scope="col" className={thClass}>Video</th>
                    <th scope="col" className={thClass}>Entity match</th>
                    <th scope="col" className={thClass}>Format</th>
                    <th scope="col" className={thClass}>Freshness</th>
                    <th scope="col" className={thClass}>Finding based on</th>
                  </tr>
                </thead>
                <tbody>
                  {d.competing_videos.map((v) => {
                    const url = safeUrl(v.url);
                    return (
                      <tr key={v.video_id}>
                        <td className={tdClass}>
                          {url ? (
                            <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
                              {v.video_id}
                              <span className="sr-only"> (opens YouTube in a new tab)</span>
                            </a>
                          ) : (
                            v.video_id
                          )}
                          <div className="text-xs text-ink-2">Published {date(v.published_at)}</div>
                        </td>
                        <td className={tdClass}>{humanize(v.entity_match)}</td>
                        <td className={tdClass}>{humanize(v.video_format)}</td>
                        <td className={tdClass}>{humanize(v.freshness)}</td>
                        <td className={tdClass}>{v.finding_basis}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-ink-2">No inspected results stored.</p>
          )}
        </Card>
        <Card title="Video ideas for this opportunity" className="lg:col-span-3">
          <RecommendationTable items={d.recommendations} caption="Recommendations for this opportunity" />
        </Card>
      </div>
      <p className="mt-6 text-xs text-muted">{d.notice}</p>
    </>
  );
}
