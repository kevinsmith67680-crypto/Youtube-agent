// Search-param handling for server pages: only known keys pass, values are bounded.

export type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export function pick(
  raw: Record<string, string | string[] | undefined>,
  keys: readonly string[],
): Record<string, string | undefined> {
  const out: Record<string, string | undefined> = {};
  for (const k of keys) {
    const v = raw[k];
    const s = Array.isArray(v) ? v[0] : v;
    if (s !== undefined && s !== "") out[k] = s.slice(0, 120);
  }
  return out;
}

export function pageNumber(v: string | undefined): number {
  const n = Number.parseInt(v ?? "1", 10);
  return Number.isFinite(n) && n >= 1 && n <= 10_000 ? n : 1;
}
