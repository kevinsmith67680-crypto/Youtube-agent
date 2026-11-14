"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DataClassBadge } from "@/components/Badges";
import { DataTable } from "@/components/charts/DataTable";
import { C, tooltipStyle } from "@/components/charts/theme";
import { InfoTip } from "@/components/ui/InfoTip";
import { age, date, humanize, num, shortDate } from "@/lib/format";
import type { SurgeEpisodeView, TrendSeriesView } from "@/lib/types";

/** One measurement per chart, on its own axis: different sources (e.g. Google Web Search
 * and YouTube Search) are never drawn on one scale. Gaps in the data stay gaps. */
export function SeriesChart({
  series,
  episodes,
}: {
  series: TrendSeriesView;
  episodes: SurgeEpisodeView[];
}) {
  const data = series.points.map((p) => ({ t: new Date(p.period_end).getTime(), v: p.value, q: p.quality }));
  const observed = data.filter((d) => d.v !== null);
  const titleId = `series-${series.series_id ?? series.label}`.replace(/[^A-Za-z0-9_-]/g, "-");
  const latest = observed.at(-1);
  const summary = observed.length
    ? `${observed.length} values from ${shortDate(observed[0]!.t)} to ${shortDate(latest!.t)} (UTC); latest ${num(latest!.v)}.`
    : "No stored values.";
  const tEnd = data.at(-1)?.t;
  const episodesInRange = episodes.filter((e) => e.start && (!series.measurement || !e.measurement || e.measurement === series.measurement));
  return (
    <figure aria-labelledby={titleId} className="rounded-lg border border-line p-3">
      <figcaption className="mb-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id={titleId} className="text-sm font-semibold text-ink">
            {humanize(series.label)}
          </h3>
          <DataClassBadge value={series.data_class} />
          <InfoTip content={series.measurement_definition}>
            <span className="text-xs text-accent underline decoration-dotted">What is measured?</span>
          </InfoTip>
        </div>
        <p className="mt-1 text-xs text-ink-2">
          {series.unit} · {series.scale_type.replace(/_/g, " ")} · {series.granularity} ·{" "}
          {series.region ?? "global"} · freshness {age(series.freshness_hours)}. {series.note}
        </p>
        <p className="sr-only">{summary}</p>
      </figcaption>
      {observed.length === 0 ? (
        <div className="rounded-md border border-dashed border-line p-6 text-center text-sm text-ink-2">
          Missing: no observations are stored for this series. Nothing is estimated in their place.
        </div>
      ) : (
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t: number) => shortDate(t)}
                tick={{ fill: C.axis, fontSize: 11 }}
                stroke={C.grid}
                minTickGap={40}
              />
              <YAxis tick={{ fill: C.axis, fontSize: 11 }} stroke={C.grid} width={44} />
              {episodesInRange.map((e) => (
                <ReferenceArea
                  key={`${e.id}-${e.start}`}
                  x1={new Date(e.start!).getTime()}
                  x2={e.end ? new Date(e.end).getTime() : tEnd}
                  fill={C.episode}
                  fillOpacity={0.12}
                  stroke="none"
                  ifOverflow="hidden"
                  label={{ value: `Surge: ${humanize(e.stage)}`, position: "insideTopLeft", fill: C.ink2, fontSize: 11 }}
                />
              ))}
              <Tooltip
                contentStyle={tooltipStyle}
                labelFormatter={(t) => `${shortDate(Number(t))} UTC`}
                formatter={(v) => [v === null || v === undefined ? "missing" : num(Number(v)), humanize(series.data_class)]}
                cursor={{ stroke: C.axis, strokeWidth: 1 }}
              />
              <Line
                type="monotone"
                dataKey="v"
                stroke={C.observed}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, stroke: C.surface, strokeWidth: 2 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      {observed.length ? (
        <DataTable
          caption={`${series.label} observations`}
          columns={["Period end (UTC)", "Value", "Quality"]}
          rows={data.slice(-200).map((d) => [date(new Date(d.t).toISOString()), d.v === null ? "missing" : num(d.v), humanize(d.q)])}
        />
      ) : null}
    </figure>
  );
}
