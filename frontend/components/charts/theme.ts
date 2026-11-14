// Chart chrome reads the same CSS variables as the page, so both themes are selected
// steps of the validated palette.
export const C = {
  grid: "rgb(var(--line))",
  axis: "rgb(var(--muted))",
  ink: "rgb(var(--ink))",
  ink2: "rgb(var(--ink-2))",
  surface: "rgb(var(--surface))",
  raised: "rgb(var(--raised))",
  observed: "rgb(var(--series-1))",
  forecast: "rgb(var(--series-2))",
  proxy: "rgb(var(--series-3))",
  episode: "rgb(var(--muted))",
};

export const tooltipStyle = {
  background: C.raised,
  border: `1px solid ${C.grid}`,
  borderRadius: 8,
  color: C.ink,
  fontSize: 12,
};
