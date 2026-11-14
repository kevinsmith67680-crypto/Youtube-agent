"use client";

import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { DataTable } from "@/components/charts/DataTable";
import { C, tooltipStyle } from "@/components/charts/theme";
import { humanize, num } from "@/lib/format";
import type { ScoreComponent } from "@/lib/types";

/** Contribution of each available component (weight x value x 100). Missing components
 * are listed as missing; they are excluded from the score, never drawn as zero. */
export function ScoreBars({ components }: { components: ScoreComponent[] }) {
  const available = components
    .filter((c) => c.contribution !== null)
    .map((c) => ({ name: humanize(c.name), value: c.contribution as number }));
  const missing = components.filter((c) => c.contribution === null);
  return (
    <figure aria-label="Score components">
      {available.length ? (
        <div style={{ height: available.length * 36 + 36 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={available} layout="vertical" margin={{ top: 4, right: 44, bottom: 4, left: 8 }}>
              <CartesianGrid horizontal={false} stroke={C.grid} />
              <XAxis type="number" tick={{ fill: C.axis, fontSize: 11 }} stroke={C.grid} />
              <YAxis type="category" dataKey="name" width={170} tick={{ fill: C.ink2, fontSize: 12 }} stroke={C.grid} />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={{ fill: C.raised }}
                formatter={(v) => [num(Number(v)), "Contribution (points)"]}
              />
              <Bar dataKey="value" fill={C.observed} barSize={18} radius={[0, 4, 4, 0]} isAnimationActive={false}>
                <LabelList dataKey="value" position="right" fill={C.ink} fontSize={12} formatter={(v: unknown) => num(Number(v))} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="text-sm text-ink-2">No component is available.</p>
      )}
      {missing.length ? (
        <ul className="mt-2 space-y-1 text-xs text-ink-2">
          {missing.map((c) => (
            <li key={c.name}>
              <span className="font-medium text-ink">{humanize(c.name)}</span>: missing, excluded from the
              score (not counted as zero). {c.note}
            </li>
          ))}
        </ul>
      ) : null}
      <DataTable
        caption="Score components"
        columns={["Component", "Value (0-1)", "Weight", "Contribution", "Note"]}
        rows={components.map((c) => [
          humanize(c.name),
          c.value === null ? "missing" : c.value.toFixed(3),
          c.weight.toFixed(2),
          c.contribution === null ? "excluded" : num(c.contribution),
          c.note,
        ])}
      />
    </figure>
  );
}
