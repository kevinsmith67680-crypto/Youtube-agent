// Browser-side mutations go to this app's own proxy route (same origin), which adds the
// API token on the server. The browser never holds a credential.

export type MutationResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string };

export async function mutate<T>(
  path: string,
  method: "POST" | "PUT" | "DELETE",
  body?: unknown,
): Promise<MutationResult<T>> {
  let res: Response;
  try {
    res = await fetch(`/api/proxy/${path}`, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 0, message: "Network error: the dashboard server is unreachable." };
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) {
    const d = data as { detail?: unknown } | null;
    const detail = d?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((x) => (x as { message?: string }).message ?? "").join("; ")
          : `Request failed (${res.status})`;
    return { ok: false, status: res.status, message };
  }
  return { ok: true, data: data as T };
}
