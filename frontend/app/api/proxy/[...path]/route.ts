import { NextResponse, type NextRequest } from "next/server";

import { apiBase, authHeaders } from "@/lib/api";
import { MAX_PROXY_BODY, allowed, sameOrigin } from "@/lib/proxy";

export const dynamic = "force-dynamic";

function error(status: number, code: string, detail: string) {
  return NextResponse.json({ error: code, detail, status }, { status });
}

async function forward(req: NextRequest, params: Promise<{ path: string[] }>) {
  const { path } = await params;
  const joined = path.join("/");
  if (!allowed(req.method, joined)) return error(404, "not_found", "no such route");
  if (!sameOrigin(req.headers.get("origin"), req.headers.get("host"))) {
    return error(403, "forbidden", "cross-origin request refused");
  }
  const type = req.headers.get("content-type") ?? "";
  let body: string | undefined;
  if (req.method !== "DELETE") {
    if (!type.startsWith("application/json")) {
      return error(415, "unsupported_media_type", "send application/json");
    }
    body = await req.text();
    if (body.length > MAX_PROXY_BODY) return error(413, "payload_too_large", "request too large");
  }
  let res: Response;
  try {
    res = await fetch(`${apiBase()}/api/${joined}`, {
      method: req.method,
      headers: { "Content-Type": "application/json", Accept: "application/json", ...authHeaders() },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(120_000),
    });
  } catch {
    return error(503, "api_unreachable", "the dashboard API is not reachable");
  }
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  return forward(req, ctx.params);
}
export async function PUT(req: NextRequest, ctx: Ctx) {
  return forward(req, ctx.params);
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  return forward(req, ctx.params);
}
