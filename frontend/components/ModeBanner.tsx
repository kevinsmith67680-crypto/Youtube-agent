import { Badge } from "@/components/ui/primitives";
import { MODE, SIGNAL_LEGEND } from "@/lib/modes";
import type { OperatingMode } from "@/lib/types";

/** The system's operating mode, always visible: what the data can and cannot support. */
export function ModeBanner({ mode }: { mode: OperatingMode | null }) {
  if (mode === null) {
    return (
      <div role="status" className="border-b border-warning/60 bg-warning/10">
        <p className="mx-auto max-w-7xl px-4 py-2 text-sm text-ink">
          Operating mode unknown: the API did not report it. Treat every signal as unverified.
        </p>
      </div>
    );
  }
  const m = MODE[mode.mode];
  const base = mode.mode === "DEGRADED" ? MODE[mode.base_mode] : null;
  return (
    <div role="status" aria-label="Operating mode" className="border-b border-line bg-surface">
      <div className="mx-auto max-w-7xl space-y-1 px-4 py-2 text-sm text-ink">
        <p className="flex flex-wrap items-center gap-2">
          <Badge tone={m.tone}>{m.label}</Badge>
          {base ? <span className="text-muted">(base: {base.label})</span> : null}
          <span>
            Direct YouTube Search surge claims:{" "}
            <strong>{mode.direct_search_claims_enabled ? "enabled" : "disabled"}</strong>
          </span>
        </p>
        <p className="text-ink-2">{base ? base.summary : m.summary}</p>
        <details className="text-ink-2">
          <summary className="cursor-pointer">Why, and what each label means</summary>
          <ul className="ml-5 list-disc">
            {mode.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {SIGNAL_LEGEND.map((s) => (
              <div key={s.key}>
                <dt className="inline font-medium text-ink">{s.label}: </dt>
                <dd className="inline">{s.description}</dd>
              </div>
            ))}
          </dl>
        </details>
      </div>
    </div>
  );
}
