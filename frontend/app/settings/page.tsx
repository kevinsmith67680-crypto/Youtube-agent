import type { Metadata } from "next";

import { AlertList } from "@/components/AlertList";
import { ErrorState } from "@/components/ErrorState";
import { ProfileForm } from "@/components/ProfileForm";
import { FreshnessTable, ProviderList } from "@/components/Tables";
import { Card, PageHeader } from "@/components/ui/primitives";
import { apiGet } from "@/lib/api";
import { date, humanize } from "@/lib/format";
import { VIDEO_FORMATS } from "@/lib/labels";
import type { AlertView, CreatorProfile, DataQualityView, Health, SystemStatusView } from "@/lib/types";

export const metadata: Metadata = { title: "Settings" };
export const dynamic = "force-dynamic";

function display(v: unknown): string {
  if (v === null || v === undefined) return "not set";
  if (typeof v === "number") return v.toLocaleString("en-GB");
  return String(v);
}

export default async function SettingsPage() {
  const [health, status, quality] = await Promise.all([
    apiGet<Health>("/health"),
    apiGet<SystemStatusView>("/system/status"),
    apiGet<DataQualityView>("/system/data-quality"),
  ]);
  const canWrite = health.ok && health.data.auth === "configured";
  const [profile, alerts] = canWrite
    ? await Promise.all([apiGet<CreatorProfile>("/profile"), apiGet<AlertView[]>("/alerts")])
    : [null, null];
  return (
    <>
      <PageHeader title="Settings">
        Your creator profile, alerts and the system&apos;s status. Provider credentials are
        configured on the server and are never shown here.
      </PageHeader>
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Creator profile" className="lg:col-span-2" id="profile">
          {profile === null ? (
            <p className="text-sm text-ink-2">The profile is private: it needs the API token.</p>
          ) : profile.ok ? (
            <ProfileForm profile={profile.data} formats={VIDEO_FORMATS} />
          ) : (
            <ErrorState {...profile} what="profile" />
          )}
        </Card>
        <Card title="System status" id="status">
          {status.ok ? (
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Mode</dt>
                <dd>
                  {humanize(status.data.mode)}
                  {status.data.synthetic ? " (synthetic)" : ""}
                </dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Version</dt>
                <dd>{status.data.version}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink-2">Database</dt>
                <dd className="text-right">{status.data.database}</dd>
              </div>
              <div>
                <dt className="text-ink-2">Concept generation</dt>
                <dd>{display(status.data.llm.mode)}</dd>
                <dd className="text-xs text-muted">
                  tokens today {display(status.data.llm.tokens_today ?? status.data.llm.calls)} / budget{" "}
                  {display(status.data.llm.daily_token_budget)}
                </dd>
              </div>
              {Object.entries(status.data.versions).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2">
                  <dt className="text-ink-2">{humanize(k)} version</dt>
                  <dd className="text-right text-xs">{v}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {status.ok ? (
            <p className="mt-2 text-xs text-muted">As of {date(status.data.generated_at)}</p>
          ) : (
            <ErrorState {...status} what="system status" />
          )}
        </Card>
        <Card title="Alert channels">
          {status.ok ? <ProviderList items={status.data.notifications} /> : null}
        </Card>
        <Card title="Alerts" className="lg:col-span-2" id="alerts">
          {alerts === null ? (
            <p className="text-sm text-ink-2">Alerts are private: they need the API token.</p>
          ) : alerts.ok ? (
            <AlertList alerts={alerts.data} canWrite={canWrite} />
          ) : (
            <ErrorState {...alerts} what="alerts" />
          )}
        </Card>
        <Card title="Data quality" className="lg:col-span-3">
          {quality.ok ? (
            <>
              <FreshnessTable items={quality.data.items} />
              {quality.data.notes.map((n) => (
                <p key={n} className="mt-2 text-xs text-muted">
                  {n}
                </p>
              ))}
            </>
          ) : (
            <ErrorState {...quality} what="data quality" />
          )}
        </Card>
      </div>
    </>
  );
}
