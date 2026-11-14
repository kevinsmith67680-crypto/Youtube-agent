"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { mutate } from "@/lib/client";

interface Result {
  considered: number;
  generated: number;
  reused: number;
  approved: number;
  research: number;
  rejected: number;
  suppressed: number;
  llm_calls: number;
  estimated_cost_usd: number | null;
  generator: string;
  skipped: string[];
  notes: string[];
  synthetic: boolean;
}

/** Bounded generation request (rate-limited and budgeted on the server). */
export function GenerateForm({ canWrite }: { canWrite: boolean }) {
  const router = useRouter();
  const [category, setCategory] = useState("all");
  const [dryRun, setDryRun] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [result, setResult] = useState<Result | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage("Generating: every idea is verified before it is stored.");
    const body = category === "all" ? { all: true, dry_run: dryRun } : { category, dry_run: dryRun };
    const r = await mutate<Result>("recommendations/generate", "POST", body);
    setBusy(false);
    if (r.ok) {
      setResult(r.data);
      setMessage("Generation finished.");
      router.refresh();
    } else {
      setResult(null);
      setMessage(r.message);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-describedby="gen-help">
      <div>
        <label htmlFor="gen-category" className="text-xs font-medium text-ink-2">
          Category
        </label>
        <select
          id="gen-category"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="mt-1 block rounded-md border border-line bg-page px-2 py-1.5 text-sm"
        >
          <option value="all">All categories</option>
          <option value="ai">Artificial intelligence</option>
          <option value="technology">Technology</option>
          <option value="gaming">Gaming</option>
        </select>
      </div>
      <div className="flex items-center gap-2 pb-1.5">
        <input
          id="gen-dry"
          type="checkbox"
          checked={dryRun}
          onChange={(e) => setDryRun(e.target.checked)}
          className="h-4 w-4"
        />
        <label htmlFor="gen-dry" className="text-sm text-ink-2">
          Dry run (nothing stored)
        </label>
      </div>
      <button
        type="submit"
        disabled={!canWrite || busy}
        className="rounded-md bg-action px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? "Generating…" : "Generate ideas"}
      </button>
      <p id="gen-help" className="w-full text-xs text-muted">
        {canWrite
          ? "Uses only stored, verified opportunities. Unchanged evidence reuses the stored ideas (no new LLM cost)."
          : "Generation needs the API token (read-only view)."}
      </p>
      <div aria-live="polite" className="w-full text-sm">
        {message ? <p className="text-ink-2">{message}</p> : null}
        {result ? (
          <p className="mt-1 text-ink">
            {result.considered} opportunities · {result.generated} new · {result.reused} reused ·{" "}
            {result.approved} approved · {result.research} research · {result.suppressed} suppressed ·
            generator {result.generator} · LLM calls {result.llm_calls}
            {result.estimated_cost_usd !== null ? ` · est. cost $${result.estimated_cost_usd.toFixed(4)}` : ""}
            {result.synthetic ? " · synthetic" : ""}
          </p>
        ) : null}
      </div>
    </form>
  );
}
