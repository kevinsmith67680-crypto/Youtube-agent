import type { Metadata } from "next";
import Link from "next/link";

import { RecStatusBadge, SyntheticBadge } from "@/components/Badges";
import { ErrorState } from "@/components/ErrorState";
import { CATEGORY_OPTIONS, FilterForm } from "@/components/FilterForm";
import { Badge, EmptyState, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, hours, humanize } from "@/lib/format";
import { pick, type SearchParams } from "@/lib/params";
import type { SavedIdea } from "@/lib/types";

export const metadata: Metadata = { title: "Saved ideas" };
export const dynamic = "force-dynamic";

const STATUS_OPTIONS = [
  { value: "", label: "Any production status" },
  ...["IDEA", "RESEARCHING", "SCRIPTING", "RECORDING", "EDITING", "READY_TO_PUBLISH", "PUBLISHED", "ABANDONED"].map((s) => ({
    value: s,
    label: humanize(s),
  })),
];

export default async function SavedPage({ searchParams }: { searchParams: SearchParams }) {
  const p = pick(await searchParams, ["status", "category"]);
  const res = await apiGet<SavedIdea[]>("/saved", p);
  return (
    <>
      <PageHeader title="Saved ideas">
        Ideas you saved, with your notes and production status. Publishing a video records
        that it exists; it is never assumed to have succeeded.
      </PageHeader>
      <FilterForm
        action="/saved"
        values={p}
        fields={[
          { kind: "select", name: "status", label: "Production status", options: STATUS_OPTIONS },
          { kind: "select", name: "category", label: "Category", options: CATEGORY_OPTIONS },
        ]}
      />
      {!res.ok ? (
        <ErrorState {...res} what="saved ideas" />
      ) : res.data.length === 0 ? (
        <EmptyState title="No saved ideas yet">
          Open a recommendation and choose <q>Save idea</q>.
        </EmptyState>
      ) : (
        <ul className="space-y-3">
          {res.data.map((s) => {
            const r = s.recommendation;
            return (
              <li key={r.id} className="rounded-xl border border-line bg-surface p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="text-base font-semibold">
                    <Link href={`/recommendations/${r.id}`} className="text-accent hover:underline">
                      {r.title}
                    </Link>
                  </h2>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="info">{s.production ? humanize(s.production.status) : "Not tracked"}</Badge>
                    <RecStatusBadge value={r.status} />
                    {r.synthetic ? <SyntheticBadge /> : null}
                  </div>
                </div>
                <p className="mt-1 text-xs text-ink-2">
                  {r.format_label} · {hours(r.hours_low, r.hours_high)} · saved {date(s.saved_at)}
                  {s.production?.published_video_id ? ` · published as ${s.production.published_video_id}` : ""}
                </p>
                {s.notes ? <p className="mt-2 text-sm text-ink">{s.notes}</p> : null}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
