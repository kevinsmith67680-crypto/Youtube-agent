// Display formatting. Missing values render as an explicit "unavailable" marker, never 0.

export const MISSING = "—";

export function humanize(value: string | null | undefined): string {
  if (!value) return MISSING;
  const s = value.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function score(v: number | null | undefined): string {
  return v === null || v === undefined ? MISSING : v.toFixed(1);
}

export function pct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? MISSING : `${(v * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return MISSING;
  return Number.isInteger(v) ? v.toLocaleString("en-GB") : v.toFixed(digits);
}

export function hours(low: number, high: number): string {
  const f = (h: number) => (Number.isInteger(h) ? String(h) : h.toFixed(1));
  return low === high ? `${f(low)} h` : `${f(low)}–${f(high)} h`;
}

const DATE = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});
const SHORT = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

export function date(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? MISSING : `${DATE.format(d)} UTC`;
}

export function shortDate(iso: string | number): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? MISSING : SHORT.format(d);
}

export function age(hoursValue: number | null | undefined): string {
  if (hoursValue === null || hoursValue === undefined) return "no data";
  if (hoursValue < 1) return "under 1 h";
  if (hoursValue < 48) return `${Math.round(hoursValue)} h`;
  return `${Math.round(hoursValue / 24)} days`;
}

/** Only http(s) URLs are rendered as links (no javascript:, data:, etc.). */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

/** Builds a query string from the current params with overrides (for links). */
export function withParams(
  base: Record<string, string | undefined>,
  overrides: Record<string, string | number | undefined>,
): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...base, ...overrides })) {
    if (v !== undefined && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}
