import clsx from "clsx";
import type { ReactNode } from "react";

import type { Tone } from "@/lib/labels";

const TONES: Record<Tone, string> = {
  neutral: "border-line text-ink-2",
  info: "border-series-1/60 text-ink",
  good: "border-good/70 text-ink",
  warning: "border-warning/80 text-ink",
  critical: "border-critical/80 text-ink",
  synthetic: "border-synthetic/80 text-ink",
};

const DOTS: Record<Tone, string> = {
  neutral: "bg-muted",
  info: "bg-series-1",
  good: "bg-good",
  warning: "bg-warning",
  critical: "bg-critical",
  synthetic: "bg-synthetic",
};

/** Text badge. Colour is never the only carrier: the label (and glyph) say it. */
export function Badge({
  children,
  tone = "neutral",
  glyph,
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  glyph?: string;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border bg-surface px-2 py-0.5 text-xs font-medium",
        TONES[tone],
        className,
      )}
    >
      <span aria-hidden="true" className={clsx("inline-block h-2 w-2 rounded-full", DOTS[tone])} />
      {glyph ? <span aria-hidden="true">{glyph}</span> : null}
      {children}
    </span>
  );
}

export function Card({
  title,
  children,
  actions,
  className,
  id,
  description,
}: {
  title?: string;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
  id?: string;
  description?: ReactNode;
}) {
  // every titled card is a named region (landmark navigation for screen readers)
  const slug = title ? title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") : undefined;
  const headingId = id ? `${id}-heading` : slug ? `card-${slug}` : undefined;
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className={clsx("rounded-xl border border-line bg-surface p-4 sm:p-5", className)}
    >
      {title ? (
        <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 id={headingId} className="text-base font-semibold text-ink">
              {title}
            </h2>
            {description ? <p className="mt-1 text-sm text-ink-2">{description}</p> : null}
          </div>
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: number | null | undefined;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="mt-1 text-3xl font-semibold text-ink">
        {value === null || value === undefined ? (
          <span className="text-xl text-muted">unavailable</span>
        ) : (
          value.toLocaleString("en-GB")
        )}
      </dd>
      {hint ? <dd className="mt-1 text-xs text-muted">{hint}</dd> : null}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line p-6 text-center">
      <p className="font-medium text-ink">{title}</p>
      {children ? <div className="mt-1 text-sm text-ink-2">{children}</div> : null}
    </div>
  );
}

export function Notice({
  children,
  tone = "info",
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}) {
  const border = {
    neutral: "border-line",
    info: "border-series-1/60",
    good: "border-good/70",
    warning: "border-warning/80",
    critical: "border-critical/80",
    synthetic: "border-synthetic/80",
  }[tone];
  return (
    <div className={clsx("rounded-lg border-l-4 bg-surface px-4 py-3 text-sm text-ink", border)}>
      {title ? <p className="font-semibold">{title}</p> : null}
      <div className={title ? "mt-1 text-ink-2" : "text-ink-2"}>{children}</div>
    </div>
  );
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold text-ink">{title}</h1>
      {children ? <div className="mt-2 max-w-3xl text-sm text-ink-2">{children}</div> : null}
    </header>
  );
}

export const tableClass = "w-full border-collapse text-left text-sm";
export const thClass = "border-b border-line px-3 py-2 text-xs font-semibold uppercase tracking-wide text-ink-2";
export const tdClass = "border-b border-line px-3 py-2 align-top";
