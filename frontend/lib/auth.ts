// Dashboard authentication (HTTP Basic), enforced by middleware.ts on every route except
// /healthz. The dashboard's server holds the API token, so whoever can load a page can act
// through it: in production the dashboard therefore refuses to serve until credentials are
// configured. Pure functions here so they can be unit-tested outside the Edge runtime.

export const MIN_PASSWORD_LENGTH = 16;

export type AuthEnv = {
  NODE_ENV?: string;
  DASHBOARD_BASIC_AUTH_USER?: string;
  DASHBOARD_BASIC_AUTH_PASSWORD?: string;
  DASHBOARD_AUTH?: string; // "disabled" is honoured only outside production
};

export type AuthDecision =
  | { kind: "allow" }
  | { kind: "challenge" }
  | { kind: "misconfigured"; reason: string };

// Compare without an early exit on the first differing character.
export function safeEqual(a: string, b: string): boolean {
  const len = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;
  for (let i = 0; i < len; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

function decodeBasic(header: string | null): [string, string] | null {
  if (!header || !header.startsWith("Basic ")) return null;
  let text: string;
  try {
    text = atob(header.slice(6).trim());
  } catch {
    return null;
  }
  const i = text.indexOf(":");
  return i < 0 ? null : [text.slice(0, i), text.slice(i + 1)];
}

export function decide(env: AuthEnv, authorization: string | null): AuthDecision {
  const production = env.NODE_ENV === "production";
  const user = env.DASHBOARD_BASIC_AUTH_USER ?? "";
  const password = env.DASHBOARD_BASIC_AUTH_PASSWORD ?? "";
  if (!user || !password) {
    if (!production && env.DASHBOARD_AUTH === "disabled") return { kind: "allow" };
    if (!production) return { kind: "allow" }; // local development: localhost only
    return {
      kind: "misconfigured",
      reason: "set DASHBOARD_BASIC_AUTH_USER and DASHBOARD_BASIC_AUTH_PASSWORD",
    };
  }
  if (production && password.length < MIN_PASSWORD_LENGTH) {
    return {
      kind: "misconfigured",
      reason: `DASHBOARD_BASIC_AUTH_PASSWORD must be at least ${MIN_PASSWORD_LENGTH} characters`,
    };
  }
  const given = decodeBasic(authorization);
  if (!given) return { kind: "challenge" };
  const okUser = safeEqual(given[0], user);
  const okPassword = safeEqual(given[1], password);
  return okUser && okPassword ? { kind: "allow" } : { kind: "challenge" };
}
