"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { mutate } from "@/lib/client";
import { humanize } from "@/lib/format";
import { FEEDBACK_CHOICES, PRODUCTION_FLOW } from "@/lib/labels";
import type { UserState } from "@/lib/types";

const button =
  "rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised disabled:cursor-not-allowed disabled:opacity-50";

/** Save / dismiss / feedback / production tracking. Every action goes through the
 * same-origin proxy; the API validates transitions (e.g. PUBLISHED needs a video ID). */
export function RecommendationActions({
  id,
  user,
  canWrite,
}: {
  id: string;
  user: UserState;
  canWrite: boolean;
}) {
  const router = useRouter();
  const [notes, setNotes] = useState(user.notes ?? "");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const current = user.production?.status ?? null;
  const nextStates = current ? (PRODUCTION_FLOW[current] ?? []) : ["IDEA", "RESEARCHING", "SCRIPTING"];
  const [status, setStatus] = useState(nextStates[0] ?? "");
  const [videoId, setVideoId] = useState("");

  async function run(label: string, path: string, method: "POST" | "DELETE", body?: unknown) {
    setBusy(true);
    const r = await mutate<UserState>(path, method, body);
    setBusy(false);
    setMessage(r.ok ? `${label}: done.` : `${label} failed: ${r.message}`);
    if (r.ok) router.refresh();
  }

  async function production(e: FormEvent) {
    e.preventDefault();
    if (!status) return;
    await run("Production status", `recommendations/${id}/production`, "POST", {
      status,
      published_video_id: status === "PUBLISHED" ? videoId.trim() : undefined,
    });
  }

  if (!canWrite) {
    return (
      <p className="text-sm text-ink-2">
        Saving, feedback and production tracking need the API token (read-only view).
      </p>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <label htmlFor="save-notes" className="text-xs font-medium text-ink-2">
          Notes (private)
        </label>
        <textarea
          id="save-notes"
          value={notes}
          maxLength={2000}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          className="mt-1 block w-full rounded-md border border-line bg-page px-2 py-1.5 text-sm"
        />
        <div className="mt-2 flex flex-wrap gap-2">
          {user.saved ? (
            <>
              <button
                type="button"
                className={button}
                disabled={busy}
                onClick={() => run("Notes", `recommendations/${id}/save`, "POST", { notes })}
              >
                Update notes
              </button>
              <button
                type="button"
                className={button}
                disabled={busy}
                onClick={() => run("Remove from saved", `recommendations/${id}/save`, "DELETE")}
              >
                Remove from saved
              </button>
            </>
          ) : (
            <button
              type="button"
              className={`${button} border-series-1`}
              disabled={busy}
              onClick={() => run("Save", `recommendations/${id}/save`, "POST", { notes: notes || null })}
            >
              Save idea
            </button>
          )}
          {!user.dismissed ? (
            <button
              type="button"
              className={button}
              disabled={busy}
              onClick={() => run("Dismiss", `recommendations/${id}/dismiss`, "POST", {})}
            >
              Dismiss
            </button>
          ) : (
            <span className="px-2 py-1.5 text-sm text-ink-2">Dismissed</span>
          )}
        </div>
      </div>

      <fieldset>
        <legend className="text-xs font-medium text-ink-2">Feedback (recorded for evaluation only)</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {FEEDBACK_CHOICES.map((f) => (
            <button
              key={f.value}
              type="button"
              className={button}
              disabled={busy}
              onClick={() =>
                run(`Feedback "${f.label}"`, `recommendations/${id}/feedback`, "POST", { feedback_type: f.value })
              }
            >
              {f.label}
              {user.feedback[f.value] ? <span className="ml-1 text-xs text-ink-2">({user.feedback[f.value]})</span> : null}
            </button>
          ))}
        </div>
      </fieldset>

      <form onSubmit={production} className="space-y-2">
        <p className="text-xs font-medium text-ink-2">
          Production status: <span className="text-ink">{current ? humanize(current) : "not tracked"}</span>
        </p>
        {nextStates.length ? (
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor="prod-status" className="text-xs text-ink-2">
                Move to
              </label>
              <select
                id="prod-status"
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="mt-1 block rounded-md border border-line bg-page px-2 py-1.5 text-sm"
              >
                {nextStates.map((s) => (
                  <option key={s} value={s}>
                    {humanize(s)}
                  </option>
                ))}
              </select>
            </div>
            {status === "PUBLISHED" ? (
              <div>
                <label htmlFor="prod-video" className="text-xs text-ink-2">
                  Published video ID
                </label>
                <input
                  id="prod-video"
                  value={videoId}
                  onChange={(e) => setVideoId(e.target.value)}
                  pattern="[A-Za-z0-9_-]{11}"
                  required
                  maxLength={11}
                  className="mt-1 block w-40 rounded-md border border-line bg-page px-2 py-1.5 text-sm"
                  aria-describedby="prod-video-help"
                />
                <p id="prod-video-help" className="text-xs text-muted">
                  The 11-character YouTube video ID.
                </p>
              </div>
            ) : null}
            <button type="submit" className={button} disabled={busy}>
              Update status
            </button>
          </div>
        ) : (
          <p className="text-sm text-ink-2">Published: no further status changes.</p>
        )}
        {user.production?.history.length ? (
          <ol className="text-xs text-muted">
            {user.production.history.map((h, i) => (
              <li key={`${h.status}-${i}`}>
                {humanize(h.status)} · {h.at.slice(0, 16).replace("T", " ")} UTC
              </li>
            ))}
          </ol>
        ) : null}
      </form>
      <p aria-live="polite" role="status" className="text-sm text-ink-2">
        {message}
      </p>
    </div>
  );
}
