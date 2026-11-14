import { NextResponse, type NextRequest } from "next/server";

import { decide } from "@/lib/auth";

export function middleware(req: NextRequest) {
  const d = decide(
    {
      NODE_ENV: process.env.NODE_ENV,
      DASHBOARD_BASIC_AUTH_USER: process.env.DASHBOARD_BASIC_AUTH_USER,
      DASHBOARD_BASIC_AUTH_PASSWORD: process.env.DASHBOARD_BASIC_AUTH_PASSWORD,
      DASHBOARD_AUTH: process.env.DASHBOARD_AUTH,
    },
    req.headers.get("authorization"),
  );
  if (d.kind === "allow") return NextResponse.next();
  if (d.kind === "misconfigured") {
    return new NextResponse(`Dashboard authentication is not configured: ${d.reason}.`, {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }
  return new NextResponse("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="trend-agent dashboard", charset="UTF-8"',
      "Cache-Control": "no-store",
    },
  });
}

export const config = {
  // everything except the container health probe
  matcher: ["/((?!healthz$).*)"],
};
