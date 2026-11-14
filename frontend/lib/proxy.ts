// The proxy's allowlist and request checks (pure functions, unit-tested).

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

const ROUTES: { method: string; pattern: RegExp }[] = [
  { method: "POST", pattern: new RegExp(`^recommendations/${UUID}/(save|dismiss|feedback|production)$`) },
  { method: "DELETE", pattern: new RegExp(`^recommendations/${UUID}/save$`) },
  { method: "POST", pattern: /^recommendations\/generate$/ },
  { method: "PUT", pattern: /^profile$/ },
  { method: "POST", pattern: new RegExp(`^alerts/${UUID}/read$`) },
];

export const MAX_PROXY_BODY = 16 * 1024;

export function allowed(method: string, path: string): boolean {
  return ROUTES.some((r) => r.method === method && r.pattern.test(path));
}

/** Same-origin check for state-changing requests: the proxy adds the API token, so a
 * request from another site must never be forwarded (CSRF). */
export function sameOrigin(origin: string | null, host: string | null): boolean {
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
