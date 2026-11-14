"use client";

import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { DataTable } from "@/components/charts/DataTable";
import { CATEGORY_LABEL } from "@/lib/labels";

/** Ranked recommendations per category: one series, one colour, value at the bar tip. */
export function CategoryBars({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data)
    .map(([k, v]) => ({ name: CATEGORY_LABEL[k] ?? k, value: v }))
    .sort((a, b) => b.value - a.value);
  if (!rows.length) return <p className="text-sm text-ink-2">No ranked recommendations yet.</p>;
  return (
    <figure aria-label="Ranked recommendations by category">
      <div style={{ height: rows.length * 44 + 40 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 40, bottom: 4, left: 8 }}>
            <CartesianGrid horizontal={false} stroke="rgb(var(--line))" />
            <XAxis type="number" allowDecimals={false} tick={{ fill: "rgb(var(--muted))", fontSize: 12 }} stroke="rgb(var(--line))" />
            <YAxis type="category" dataKey="name" width={150} tick={{ fill: "rgb(var(--ink-2))", fontSize: 12 }} stroke="rgb(var(--line))" />
            <Tooltip
              cursor={{ fill: "rgb(var(--raised))" }}
              contentStyle={{ background: "rgb(var(--raised))", border: "1px solid rgb(var(--line))", color: "rgb(var(--ink))" }}
              formatter={(v) => [String(v), "Ranked ideas"]}
            />
            <Bar dataKey="value" fill="rgb(var(--series-1))" barSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}>
              <LabelList dataKey="value" position="right" fill="rgb(var(--ink))" fontSize={12} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <DataTable caption="Ranked recommendations by category" columns={["Category", "Ranked ideas"]} rows={rows.map((r) => [r.name, r.value])} />
    </figure>
  );
}
