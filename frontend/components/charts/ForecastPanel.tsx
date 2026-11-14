"use client";

import * as Tabs from "@radix-ui/react-tabs";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DataClassBadge } from "@/components/Badges";
import { DataTable } from "@/components/charts/DataTable";
import { C, tooltipStyle } from "@/components/charts/theme";
import { Badge, Notice } from "@/components/ui/primitives";
import { date, humanize, num, pct, shortDate } from "@/lib/format";
import type { ForecastView, TrendSeriesView } from "@/lib/types";

const UNAVAILABLE = new Set([
  "INSUFFICIENT_HISTORY",
  "UNSUPPORTED_GRANULARITY",
  "SOURCE_UNAVAILABLE",
  "OUT_OF_DISTRIBUTION",
  "FORECAST_UNAVAILABLE",
]);

export function hasNumericForecast(f: ForecastView): boolean {
  return !UNAVAILABLE.has(f.status) && f.trajectory.some((p) => p.value !== null);
}

function Unavailable({ f }: { f: ForecastView }) {
  return (
    <div role="status" className="rounded-md border border-dashed border-line p-4 text-sm">
      <p className="font-medium text-ink">Forecast unavailable: {humanize(f.status)}</p>
      {f.status_reasons.length ? (
        <ul className="mt-1 list-disc pl-5 text-ink-2">
          {f.status_reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      <p className="mt-2 text-xs text-muted">
        No forecast line is drawn. A missing forecast is not a prediction of zero growth.
      </p>
    </div>
  );
}

function ForecastChart({ f, observed }: { f: ForecastView; observed: TrendSeriesView | null }) {
  const cutoff = new Date(f.data_cutoff).getTime();
  const obs = (observed?.points ?? [])
    .map((p) => ({ t: new Date(p.period_end).getTime(), observed: p.value }))
    .filter((p) => p.t <= cutoff)
    .slice(-72);
  const fc = f.trajectory.map((p) => ({
    t: new Date(p.forecast_time).getTime(),
    forecast: p.value,
    band: p.lower !== null && p.upper !== null ? [p.lower, p.upper] : null,
  }));
  // The forecast line starts at the last observation (same measured series), so a
  // single-step forecast is a visible segment rather than an isolated point.
  const last = [...obs].reverse().find((p) => p.observed !== null);
  const anchor =
    last && fc.length && fc[0]!.t > last.t
      ? [{ t: last.t, observed: last.observed, forecast: last.observed, band: [last.observed!, last.observed!] }]
      : [];
  const data = [...obs.filter((p) => !last || p.t !== last.t), ...anchor, ...fc].sort((a, b) => a.t - b.t);
  const level = f.trajectory.find((p) => p.interval_level !== null)?.interval_level ?? null;
  const intervalName = level ? `${Math.round(level * 100)}% interval` : "Interval";
  return (
    <>
      <div className="h-60">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
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
              padding={{ left: 0, right: 16 }}
            />
            <YAxis tick={{ fill: C.axis, fontSize: 11 }} stroke={C.grid} width={44} />
            <ReferenceLine x={cutoff} stroke={C.axis} label={{ value: "data cutoff", fill: C.ink2, fontSize: 11, position: "insideTopRight" }} />
            {f.threshold_index !== null ? (
              <ReferenceLine
                y={f.threshold_index}
                stroke={C.axis}
                strokeDasharray="4 4"
                label={{ value: "breakout threshold", fill: C.ink2, fontSize: 11, position: "insideBottomLeft" }}
              />
            ) : null}
            <Tooltip
              contentStyle={tooltipStyle}
              labelFormatter={(t) => `${shortDate(Number(t))} UTC`}
              formatter={(v, name) => {
                if (Array.isArray(v)) return [`${num(Number(v[0]))}–${num(Number(v[1]))}`, name];
                return [v === null || v === undefined ? "missing" : num(Number(v)), name];
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12, color: C.ink2 }} />
            <Area
              dataKey="band"
              name={intervalName}
              stroke="none"
              fill={C.forecast}
              fillOpacity={0.12}
              isAnimationActive={false}
              connectNulls={false}
              legendType="rect"
            />
            {obs.length ? (
              <Line
                dataKey="observed"
                name={`Observed (${observed?.data_class === "SYNTHETIC" ? "synthetic" : "observed"})`}
                stroke={C.observed}
                strokeWidth={2}
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
                legendType="line"
              />
            ) : null}
            <Line
              dataKey="forecast"
              name="Forecast (median)"
              stroke={C.forecast}
              strokeWidth={2}
              strokeDasharray="6 4"
              dot={{ r: 4, fill: C.forecast, stroke: C.surface, strokeWidth: 2 }}
              connectNulls={false}
              isAnimationActive={false}
              legendType="line"
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <DataTable
        caption={`Forecast trajectory, ${f.horizon_hours} hours`}
        columns={["Time (UTC)", "Lead (h)", "Median", "Lower", "Upper", "Method"]}
        rows={f.trajectory.map((p) => [date(p.forecast_time), p.lead_hours, num(p.value), num(p.lower), num(p.upper), p.method])}
      />
    </>
  );
}

function ForecastBody({ f, observed, compact }: { f: ForecastView; observed: TrendSeriesView | null; compact: boolean }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <DataClassBadge value={f.synthetic ? "SYNTHETIC" : "FORECAST"} />
        <Badge>{humanize(f.status)}</Badge>
        {f.is_proxy ? <Badge tone="warning">Proxy attention, not search demand</Badge> : null}
        <span className="text-muted">
          issued {date(f.issued_at)} · data to {date(f.data_cutoff)}
        </span>
      </div>
      {hasNumericForecast(f) ? (
        <>
          {!compact ? <ForecastChart f={f} observed={observed} /> : null}
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <div>
              <dt className="text-xs text-ink-2">Breakout probability</dt>
              <dd className="tabular">
                {f.breakout_probability !== null ? pct(f.breakout_probability) : "not available"}
                {f.breakout_calibration ? <span className="ml-1 text-xs text-ink-2">({humanize(f.breakout_calibration)})</span> : null}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-ink-2">Continuation probability</dt>
              <dd className="tabular">{f.continuation_probability !== null ? pct(f.continuation_probability) : "not available"}</dd>
            </div>
            <div>
              <dt className="text-xs text-ink-2">Peak timing</dt>
              <dd>
                {f.peak && f.peak.earliest_hours !== null
                  ? `${num(f.peak.earliest_hours, 0)}–${num(f.peak.latest_hours, 0)} h (${humanize(f.peak.status)})`
                  : f.peak
                    ? humanize(f.peak.status)
                    : "not estimated"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-ink-2">Model</dt>
              <dd className="text-xs">{f.model ?? "baseline"}</dd>
            </div>
          </dl>
          {f.windows.length ? (
            <div>
              <p className="text-xs font-medium text-ink-2">Planning windows (hypothetical, by production profile)</p>
              <ul className="mt-1 text-sm">
                {f.windows.map((w) => (
                  <li key={w.production_profile} className="flex flex-wrap justify-between gap-2 border-b border-line py-1">
                    <span>{humanize(w.production_profile)} ({num(w.production_hours, 0)} h)</span>
                    <span className="text-ink-2">
                      {humanize(w.status)} · elevated at publication {pct(w.probability_elevated_at_publication)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : (
        <Unavailable f={f} />
      )}
      <p className="text-xs text-muted">{f.notice}</p>
    </div>
  );
}

export function ForecastPanel({
  forecasts,
  observed,
  compact = false,
}: {
  forecasts: ForecastView[];
  observed: TrendSeriesView | null;
  compact?: boolean;
}) {
  if (!forecasts.length) {
    return (
      <Notice tone="neutral" title="No forecast stored">
        No valid numerical forecast exists for this query, so none is drawn. This is not a
        prediction of zero growth.
      </Notice>
    );
  }
  const sorted = [...forecasts].sort((a, b) => a.horizon_hours - b.horizon_hours);
  const first = sorted.find((f) => f.horizon_hours === 24) ?? sorted[0]!;
  return (
    <Tabs.Root defaultValue={first.prediction_id}>
      <Tabs.List aria-label="Forecast horizon" className="mb-3 flex gap-1">
        {sorted.map((f) => (
          <Tabs.Trigger
            key={f.prediction_id}
            value={f.prediction_id}
            className="rounded-md border border-line px-3 py-1 text-sm text-ink-2 data-[state=active]:bg-raised data-[state=active]:font-semibold data-[state=active]:text-ink"
          >
            {f.horizon_hours} h
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      {sorted.map((f) => (
        <Tabs.Content key={f.prediction_id} value={f.prediction_id}>
          <ForecastBody f={f} observed={observed} compact={compact} />
        </Tabs.Content>
      ))}
    </Tabs.Root>
  );
}
