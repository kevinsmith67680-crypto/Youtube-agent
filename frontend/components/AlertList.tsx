"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, EmptyState } from "@/components/ui/primitives";
import { mutate } from "@/lib/client";
import { date, humanize } from "@/lib/format";
import type { AlertView } from "@/lib/types";

export function AlertList({ alerts, canWrite }: { alerts: AlertView[]; canWrite: boolean }) {
  const router = useRouter();
  const [message, setMessage] = useState("");
  if (!alerts.length) return <EmptyState title="No alerts">New strong opportunities and ideas will appear here.</EmptyState>;
  async function markRead(id: string) {
    const r = await mutate(`alerts/${id}/read`, "POST", {});
    setMessage(r.ok ? "Alert marked as read." : r.message);
    if (r.ok) router.refresh();
  }
  return (
    <div>
      <ul className="divide-y divide-line">
        {alerts.map((a) => (
          <li key={a.id} className="py-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className={a.read ? "text-sm text-ink-2" : "text-sm font-semibold text-ink"}>{a.title}</p>
                <p className="mt-1 text-xs text-ink-2">{a.body}</p>
                <p className="mt-1 text-xs text-muted">
                  {humanize(a.event_type)} · {date(a.created_at)}
                  {a.deliveries.length
                    ? ` · delivery: ${a.deliveries.map((d) => `${d.channel} ${humanize(d.status).toLowerCase()}`).join(", ")}`
                    : ""}
                </p>
                <div className="mt-1 flex gap-3 text-xs">
                  {a.recommendation_id ? (
                    <Link className="text-accent hover:underline" href={`/recommendations/${a.recommendation_id}`}>
                      Open idea
                    </Link>
                  ) : null}
                  {a.opportunity_id ? (
                    <Link className="text-accent hover:underline" href={`/opportunities/${a.opportunity_id}`}>
                      Open opportunity
                    </Link>
                  ) : null}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {a.synthetic ? <Badge tone="synthetic">Synthetic</Badge> : null}
                {a.read ? (
                  <Badge>Read</Badge>
                ) : canWrite ? (
                  <button
                    type="button"
                    onClick={() => markRead(a.id)}
                    className="rounded-md border border-line px-2 py-1 text-xs hover:bg-raised"
                  >
                    Mark as read<span className="sr-only">: {a.title}</span>
                  </button>
                ) : (
                  <Badge tone="info">Unread</Badge>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
      <p aria-live="polite" className="mt-2 text-xs text-ink-2">
        {message}
      </p>
    </div>
  );
}
