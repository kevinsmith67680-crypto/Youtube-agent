"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { mutate } from "@/lib/client";
import { humanize } from "@/lib/format";
import { CATEGORIES, CATEGORY_LABEL } from "@/lib/labels";
import type { CreatorProfile } from "@/lib/types";

const input = "mt-1 block w-full rounded-md border border-line bg-page px-2 py-1.5 text-sm text-ink";

function splitList(v: string): string[] {
  return v
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function numberOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Only what the creator states is used; nothing about the channel is inferred. */
export function ProfileForm({ profile, formats }: { profile: CreatorProfile; formats: string[] }) {
  const router = useRouter();
  const [p, setP] = useState(profile);
  const [lists, setLists] = useState({
    primary_regions: profile.primary_regions.join(", "),
    skills: profile.skills.join(", "),
    equipment: profile.equipment.join(", "),
    content_exclusions: profile.content_exclusions.join(", "),
    topic_preferences: profile.topic_preferences.join(", "),
  });
  const [nums, setNums] = useState({
    available_hours_per_week: profile.available_hours_per_week?.toString() ?? "",
    production_capacity_videos_per_week: profile.production_capacity_videos_per_week?.toString() ?? "",
    preferred_duration_minutes: profile.preferred_duration_minutes?.toString() ?? "",
  });
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  function toggle(key: "preferred_categories" | "preferred_formats", value: string) {
    setP((prev) => {
      const cur = prev[key];
      return { ...prev, [key]: cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value] };
    });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body: CreatorProfile = {
      ...p,
      primary_regions: splitList(lists.primary_regions).map((r) => r.toUpperCase()),
      skills: splitList(lists.skills),
      equipment: splitList(lists.equipment),
      content_exclusions: splitList(lists.content_exclusions),
      topic_preferences: splitList(lists.topic_preferences),
      available_hours_per_week: numberOrNull(nums.available_hours_per_week),
      production_capacity_videos_per_week: numberOrNull(nums.production_capacity_videos_per_week),
      preferred_duration_minutes: numberOrNull(nums.preferred_duration_minutes),
    };
    const r = await mutate<CreatorProfile>("profile", "PUT", body);
    setBusy(false);
    setMessage(r.ok ? "Profile saved. New generations use it; existing ideas are unchanged." : `Not saved: ${r.message}`);
    if (r.ok) router.refresh();
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="pf-name" className="text-xs font-medium text-ink-2">
            Display name
          </label>
          <input
            id="pf-name"
            value={p.display_name}
            maxLength={80}
            onChange={(e) => setP({ ...p, display_name: e.target.value })}
            className={input}
          />
        </div>
        <div>
          <label htmlFor="pf-regions" className="text-xs font-medium text-ink-2">
            Primary regions (two-letter codes, comma-separated)
          </label>
          <input
            id="pf-regions"
            value={lists.primary_regions}
            onChange={(e) => setLists({ ...lists, primary_regions: e.target.value })}
            className={input}
          />
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="pf-style" className="text-xs font-medium text-ink-2">
            Production style
          </label>
          <input
            id="pf-style"
            value={p.production_style}
            maxLength={200}
            onChange={(e) => setP({ ...p, production_style: e.target.value })}
            className={input}
          />
        </div>
      </div>

      <fieldset>
        <legend className="text-xs font-medium text-ink-2">Categories</legend>
        <div className="mt-2 flex flex-wrap gap-4">
          {CATEGORIES.map((c) => (
            <label key={c} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={p.preferred_categories.includes(c)}
                onChange={() => toggle("preferred_categories", c)}
                className="h-4 w-4"
              />
              {CATEGORY_LABEL[c]}
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend className="text-xs font-medium text-ink-2">Preferred formats (optional)</legend>
        <div className="mt-2 grid max-h-56 gap-1 overflow-auto rounded-md border border-line p-2 sm:grid-cols-2">
          {formats.map((f) => (
            <label key={f} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={p.preferred_formats.includes(f)}
                onChange={() => toggle("preferred_formats", f)}
                className="h-4 w-4"
              />
              {humanize(f)}
            </label>
          ))}
        </div>
      </fieldset>

      <div className="grid gap-4 sm:grid-cols-3">
        {(
          [
            ["available_hours_per_week", "Hours available per week", 168],
            ["production_capacity_videos_per_week", "Videos per week", 50],
            ["preferred_duration_minutes", "Preferred length (minutes)", 240],
          ] as const
        ).map(([key, label, max]) => (
          <div key={key}>
            <label htmlFor={`pf-${key}`} className="text-xs font-medium text-ink-2">
              {label}
            </label>
            <input
              id={`pf-${key}`}
              type="number"
              min={0}
              max={max}
              step="any"
              value={nums[key]}
              onChange={(e) => setNums({ ...nums, [key]: e.target.value })}
              className={input}
            />
          </div>
        ))}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {(
          [
            ["skills", "Skills"],
            ["equipment", "Equipment you have"],
            ["topic_preferences", "Topics you prefer"],
            ["content_exclusions", "Topics to exclude"],
          ] as const
        ).map(([key, label]) => (
          <div key={key}>
            <label htmlFor={`pf-${key}`} className="text-xs font-medium text-ink-2">
              {label} (comma-separated)
            </label>
            <input
              id={`pf-${key}`}
              value={lists[key]}
              onChange={(e) => setLists({ ...lists, [key]: e.target.value })}
              className={input}
            />
          </div>
        ))}
      </div>

      <fieldset>
        <legend className="text-xs font-medium text-ink-2">Alert channels</legend>
        <p className="text-xs text-muted">
          Email and Telegram deliver only when their credentials are configured on the server. Synthetic data is
          never sent outside the dashboard.
        </p>
        <div className="mt-2 flex flex-wrap gap-4">
          {["in_app", "email", "telegram"].map((ch) => (
            <label key={ch} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={!!p.notification_preferences[ch]}
                onChange={(e) =>
                  setP({ ...p, notification_preferences: { ...p.notification_preferences, [ch]: e.target.checked } })
                }
                className="h-4 w-4"
              />
              {humanize(ch)}
            </label>
          ))}
        </div>
      </fieldset>

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save profile"}
        </button>
        <p aria-live="polite" role="status" className="text-sm text-ink-2">
          {message}
        </p>
      </div>
    </form>
  );
}
