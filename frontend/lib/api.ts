import "server-only";

import type { ApiErrorBody } from "./types";

// Server-side access to the dashboard API. The token lives in the Next.js server's
// environment only; it is never serialised into a page or sent to the browser.

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string; detail: string };

type Params = Record<string, string | number | boolean | null | undefined>;

export function apiBase(): string {
  return (process.env.TREND_AGENT_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
}

export function authHeaders(): Record<string, string> {
  const token = process.env.TREND_AGENT_API_TOKEN;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) =>
        d && typeof d === "object" && "message" in d
          ? `${String((d as { location?: string }).location ?? "")}: ${String((d as { message: string }).message)}`
          : String(d),
      )
      .join("; ");
  }
  return "";
}

export async function apiGet<T>(path: string, params?: Params): Promise<ApiResult<T>> {
  if (!path.startsWith("/") || path.includes("..")) {
    return { ok: false, status: 400, error: "invalid_path", detail: "invalid API path" };
  }
  const url = new URL(`${apiBase()}/api${path}`);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: "application/json", ...authHeaders() },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    return {
      ok: false,
      status: 503,
      error: "api_unreachable",
      detail:
        "The dashboard API is not reachable. Start it with `trend-agent dashboard serve` " +
        "(or `--demo`) and check TREND_AGENT_API_URL.",
    };
  }
  if (!res.ok) {
    let body: Partial<ApiErrorBody> = {};
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      /* non-JSON error body */
    }
    return {
      ok: false,
      status: res.status,
      error: body.error ?? "error",
      detail: describe(body.detail) || res.statusText,
    };
  }
  return { ok: true, data: (await res.json()) as T };
}
