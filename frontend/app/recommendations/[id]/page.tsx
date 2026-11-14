import type { Metadata } from "next";
import Link from "next/link";

import { RecStatusBadge, StatementBadge, SyntheticBadge } from "@/components/Badges";
import { ScoreBars } from "@/components/charts/ScoreBars";
import { ErrorState } from "@/components/ErrorState";
import { RecommendationActions } from "@/components/RecommendationActions";
import { Badge, Card, Notice, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, hours, humanize, num, pct, safeUrl, score } from "@/lib/format";
import { STATEMENT, URGENCY } from "@/lib/labels";
import type { Health, RecommendationDetail, StatementLabel } from "@/lib/types";

export const metadata: Metadata = { title: "Recommendation" };
export const dynamic = "force-dynamic";

function List({ items, empty = "None." }: { items: string[]; empty?: string }) {
  if (!items.length) return <p className="text-sm text-ink-2">{empty}</p>;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-ink">
      {items.map((x) => (
        <li key={x}>{x}</li>
      ))}
    </ul>
  );
}

function yesNoUnknown(v: boolean | null): string {
  return v === null ? "Unknown (not in your creator profile)" : v ? "Yes" : "No";
}

export default async function RecommendationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [res, health] = await Promise.all([
    apiGet<RecommendationDetail>(`/recommendations/${encodeURIComponent(id)}`, { track_view: true }),
    apiGet<Health>("/health"),
  ]);
  if (!res.ok) {
    return (
      <>
        <PageHeader title="Recommendation" />
        <ErrorState {...res} what="recommendation" />
      </>
    );
  }
  const canWrite = health.ok && health.data.auth === "configured";
  const { recommendation: r, user, opportunity } = res.data;
  const p = r.production;
  const rk = r.ranking;
  const v = r.verification;
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/recommendations" className="text-accent hover:underline">
          Recommendations
        </Link>
        {opportunity ? (
          <>
            <span aria-hidden="true" className="px-2 text-muted">/</span>
            <Link href={`/opportunities/${opportunity.id}`} className="text-accent hover:underline">
              {opportunity.query}
            </Link>
          </>
        ) : null}
      </nav>
      <PageHeader title={r.title}>
        <div className="flex flex-wrap items-center gap-2">
          <RecStatusBadge value={v.status} />
          <Badge tone="info">Score {score(rk.composite_score)}</Badge>
          {rk.rank ? <Badge>Rank {rk.rank}</Badge> : null}
          <Badge>{r.format_label}</Badge>
          {r.research_hypothesis ? <Badge tone="warning">Research candidate</Badge> : null}
          {r.suppression.suppressed ? <Badge>Suppressed: {r.suppression.reason}</Badge> : null}
          {r.synthetic ? <SyntheticBadge /> : null}
        </div>
      </PageHeader>
      {v.labels.length ? (
        <div className="mb-4 flex flex-wrap gap-2">
          {v.labels.map((l) => (
            <Badge key={l} tone={l.includes("SYNTHETIC") ? "synthetic" : "warning"}>
              {l}
            </Badge>
          ))}
        </div>
      ) : null}
      {r.research_hypothesis || v.status === "RESEARCH_REQUIRED" ? (
        <div className="mb-4">
          <Notice tone="warning" title="Research before filming">
            The evidence is not yet sufficient to recommend producing this video. Treat it as a
            question to investigate.
          </Notice>
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Concept">
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-ink-2">Search query</dt>
                <dd>{r.primary_query}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-2">Target intent</dt>
                <dd>
                  {humanize(r.target_intent)}
                  {r.target_sub_intent ? ` · ${humanize(r.target_sub_intent)}` : ""}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-ink-2">Target audience</dt>
                <dd>{r.target_audience}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-ink-2">Viewer problem</dt>
                <dd>{r.core_viewer_problem}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-ink-2">Premise</dt>
                <dd>{r.premise}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-ink-2">Why it may satisfy demand</dt>
                <dd>{r.why_it_may_satisfy_demand}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-ink-2">Format rationale</dt>
                <dd>{r.format_rationale}</dd>
              </div>
            </dl>
          </Card>

          <Card title="Title options" description="Search-oriented titles by strategy. No statistics or unverifiable promises.">
            <ul className="divide-y divide-line">
              <li className="py-2">
                <p className="font-medium text-ink">{r.title}</p>
                <p className="text-xs text-ink-2">Primary title · {r.title.length} characters</p>
              </li>
              {r.alternative_titles.map((t) => (
                <li key={t.text} className="py-2">
                  <p className={t.accepted ? "text-ink" : "text-ink-2 line-through"}>{t.text}</p>
                  <p className="text-xs text-ink-2">
                    {humanize(t.strategy)} · {t.length} characters · covers {pct(t.query_term_coverage)} of the query terms
                    {t.reasons.length ? ` · ${t.reasons.join("; ")}` : ""}
                  </p>
                </li>
              ))}
            </ul>
          </Card>

          <Card title="Opening hooks">
            <ol className="space-y-3">
              {r.hooks.map((h, i) => (
                <li key={h.text} className="rounded-lg border border-line p-3">
                  <p className="text-xs text-ink-2">
                    Hook {i + 1} · {humanize(h.hook_type)} · about {num(h.estimated_seconds, 0)} s
                  </p>
                  <p className="mt-1 text-sm text-ink">{h.text}</p>
                  {h.reasons.length ? <p className="mt-1 text-xs text-muted">{h.reasons.join("; ")}</p> : null}
                </li>
              ))}
            </ol>
          </Card>

          <Card
            title="Outline"
            description="Every statement carries a label. Only verified facts may be presented as facts in the video."
          >
            <dl className="mb-4 grid gap-2 text-xs sm:grid-cols-2">
              {(Object.keys(STATEMENT) as StatementLabel[]).map((k) => (
                <div key={k} className="flex items-start gap-2">
                  <dt>
                    <StatementBadge value={k} />
                  </dt>
                  <dd className="text-ink-2">{STATEMENT[k].description}</dd>
                </div>
              ))}
            </dl>
            <ol className="space-y-4">
              {r.outline.sections.map((s, i) => (
                <li key={`${s.heading}-${i}`}>
                  <h3 className="text-sm font-semibold text-ink">
                    {i + 1}. {s.heading}
                  </h3>
                  <p className="text-xs text-ink-2">{s.purpose}</p>
                  <ul className="mt-2 space-y-2">
                    {s.items.map((it, j) => (
                      <li key={`${it.text}-${j}`} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
                        <span className="shrink-0">
                          <StatementBadge value={it.label} />
                        </span>
                        <span className="text-sm text-ink">
                          {it.text}
                          {it.note ? <span className="block text-xs text-muted">{it.note}</span> : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ol>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-2">Verify before publishing</h3>
                <List items={r.outline.required_evidence} />
              </div>
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-2">Likely failure points</h3>
                <List items={r.outline.failure_points} />
              </div>
            </div>
            <p className="mt-3 text-sm text-ink-2">
              <span className="font-medium text-ink">Conclusion to investigate: </span>
              {r.outline.conclusion_to_investigate}
            </p>
          </Card>

          <Card title="Key questions and information needs">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-2">Questions viewers ask</h3>
                <List items={r.key_questions} />
              </div>
              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-2">Information requirements</h3>
                <List items={r.key_information_requirements} />
              </div>
            </div>
          </Card>

          <Card title="Alternative approaches">
            {r.alternative_approaches.length ? (
              <ul className="divide-y divide-line text-sm">
                {r.alternative_approaches.map((a) => (
                  <li key={a.video_format} className="py-2">
                    <span className="font-medium">{a.label}</span>
                    {a.preferred_by_creator ? <Badge className="ml-2">Your preferred format</Badge> : null}
                    <p className="text-xs text-ink-2">{a.rationale}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-ink-2">No alternative format serves this intent.</p>
            )}
          </Card>

          <Card title="Evidence">
            {r.evidence.length ? (
              <ul className="divide-y divide-line text-sm">
                {r.evidence.map((e) => {
                  const url = safeUrl(e.url);
                  return (
                    <li key={e.evidence_id} className="py-2">
                      <span className="font-medium">{humanize(e.kind)}</span>
                      {e.publisher ? <span className="text-ink-2"> · {e.publisher}</span> : null}
                      {e.trust ? <span className="text-ink-2"> · {humanize(e.trust)}</span> : null}
                      {url ? (
                        <>
                          {" · "}
                          <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">
                            source<span className="sr-only"> (opens in a new tab)</span>
                          </a>
                        </>
                      ) : null}
                      {e.note ? <p className="text-xs text-ink-2">{e.note}</p> : null}
                      <p className="text-xs text-muted">{e.evidence_id}</p>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-sm text-ink-2">No evidence records are cited.</p>
            )}
            {r.competing_videos.length ? (
              <p className="mt-3 text-xs text-ink-2">
                Inspected competing videos: {r.competing_videos.length} (see the opportunity page).
              </p>
            ) : null}
          </Card>
        </div>

        <div className="space-y-6">
          <Card title="Your decision">
            <RecommendationActions id={r.recommendation_id} user={user} canWrite={canWrite} />
          </Card>

          <Card title="Feasibility">
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Estimated effort</dt>
                <dd className="tabular">{hours(p.estimated_hours_low, p.estimated_hours_high)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Profile</dt>
                <dd>
                  {humanize(p.production_profile)} ({num(p.profile_hours, 0)} h planning assumption)
                </dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Complexity</dt>
                <dd>{humanize(p.complexity)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Urgency</dt>
                <dd>{URGENCY[p.urgency] ?? humanize(p.urgency)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">You have the requirements</dt>
                <dd>{yesNoUnknown(p.creator_has_requirements)}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Fits your capacity</dt>
                <dd>{yesNoUnknown(p.fits_creator_capacity)}</dd>
              </div>
            </dl>
            {p.urgency_reasons.length ? <p className="mt-2 text-xs text-ink-2">{p.urgency_reasons.join(" ")}</p> : null}
            <h3 className="mt-3 text-xs font-semibold uppercase tracking-wide text-ink-2">Time breakdown</h3>
            <ul className="text-sm">
              {Object.entries(p.breakdown_hours).map(([k, h]) => (
                <li key={k} className="flex justify-between border-b border-line py-1">
                  <span>{humanize(k)}</span>
                  <span className="tabular">{num(h)} h</span>
                </li>
              ))}
            </ul>
            {p.required_software.length || p.required_hardware.length ? (
              <p className="mt-2 text-xs text-ink-2">
                Needs: {[...p.required_software, ...p.required_hardware].join(", ")}
              </p>
            ) : null}
            {p.notes.length ? <p className="mt-2 text-xs text-muted">{p.notes.join(" ")}</p> : null}
          </Card>

          <Card title="Why this rank" description={rk.note}>
            <ScoreBars components={rk.components} />
            <p className="mt-2 text-sm text-ink-2">{rk.explanation}</p>
            <p className="mt-1 text-xs text-muted">
              Ranking {rk.version} · available weight {pct(rk.available_weight)} · evidence-quality factor{" "}
              {rk.evidence_quality_factor.toFixed(2)}
            </p>
          </Card>

          <Card title="Demand and forecast">
            <p className="text-sm text-ink">{r.demand.statement}</p>
            {r.demand.indirect ? (
              <p className="mt-1 text-xs text-ink-2">Indirect demand evidence: not a YouTube Search measurement.</p>
            ) : null}
            <div className="mt-3 text-sm">
              {r.forecast.available ? (
                <p>
                  {r.forecast.horizon_hours} h forecast ({humanize(r.forecast.status)}): breakout{" "}
                  {r.forecast.breakout_probability !== null ? pct(r.forecast.breakout_probability) : "not available"}
                  {r.forecast.breakout_calibration ? ` (${humanize(r.forecast.breakout_calibration)})` : ""}
                  {r.forecast.is_proxy ? " · proxy attention" : ""}
                </p>
              ) : (
                <p className="text-ink-2">
                  No usable forecast: the forecast component is excluded from the score, not set to zero.
                </p>
              )}
              <p className="mt-1 text-xs text-muted">{r.forecast.note}</p>
            </div>
          </Card>

          <Card title="Verification">
            <p className="text-sm text-ink-2">
              Application code decided this status; the generator cannot approve its own claims. Verified{" "}
              {date(v.verified_at)}.
            </p>
            {v.trend_statement ? <p className="mt-2 text-sm text-ink">{v.trend_statement}</p> : null}
            <ul className="mt-2 space-y-1 text-xs">
              {v.checks.map((c) => (
                <li key={c.check}>
                  <span aria-hidden="true">{c.passed ? "✓" : "✕"}</span>{" "}
                  <span className="font-medium">{c.passed ? "Passed" : "Failed"}</span>: {humanize(c.check)}
                  {c.detail ? <span className="text-ink-2"> — {c.detail}</span> : null}
                </li>
              ))}
            </ul>
            {v.removed.length ? (
              <>
                <h3 className="mt-3 text-xs font-semibold uppercase tracking-wide text-ink-2">Removed during verification</h3>
                <List items={v.removed} />
              </>
            ) : null}
            {r.uncertainties.length ? (
              <>
                <h3 className="mt-3 text-xs font-semibold uppercase tracking-wide text-ink-2">Uncertainties</h3>
                <List items={r.uncertainties} />
              </>
            ) : null}
          </Card>
        </div>
      </div>
      <p className="mt-6 text-xs text-muted">
        {r.notice} Generated {date(r.generated_at)} by {r.generator}.
      </p>
    </>
  );
}
