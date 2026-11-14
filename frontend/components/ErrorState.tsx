import { Notice } from "@/components/ui/primitives";

export function ErrorState({
  status,
  error,
  detail,
  what = "data",
}: {
  status: number;
  error: string;
  detail: string;
  what?: string;
}) {
  if (status === 401) {
    return (
      <Notice tone="warning" title={`This ${what} needs the dashboard API token`}>
        The API refused the request. Set the same <code>TREND_AGENT_API_TOKEN</code> for the API
        server and for this dashboard&apos;s server (it is never sent to the browser).
      </Notice>
    );
  }
  if (status === 404) {
    return (
      <Notice tone="neutral" title="Not found">
        This record does not exist, has expired under the retention policy, or is synthetic and
        not shown in production mode.
      </Notice>
    );
  }
  if (error === "api_unreachable" || status === 503) {
    return (
      <Notice tone="critical" title="Dashboard API unavailable">
        {detail} No data is shown rather than stale or estimated values.
      </Notice>
    );
  }
  if (status === 429) {
    return (
      <Notice tone="warning" title="Rate limited">
        Too many requests. Wait a moment and reload.
      </Notice>
    );
  }
  return (
    <Notice tone="critical" title={`Could not load ${what} (${status})`}>
      {detail || error}
    </Notice>
  );
}
