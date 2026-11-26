"use client";

import { useState } from "react";
import { BreakdownBars, DailySpendChart } from "@/components/charts";
import { ProviderEditor, ProviderList, type ProviderOps } from "@/components/providers";
import { AdminOnly, useSession } from "@/components/session";
import {
  Badge,
  BudgetBar,
  Button,
  Card,
  CardHeader,
  DataView,
  PageHeader,
  Select,
  Stat,
} from "@/components/ui";
import { api } from "@/lib/api";
import { usd } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

const ops: ProviderOps = {
  create: api.createProvider,
  update: api.updateProvider,
  remove: api.deleteProvider,
  test: api.testProvider,
};

export default function AiPage() {
  const { canAdmin } = useSession();
  const providers = useAsync(() => api.providers(), "providers");
  const [days, setDays] = useState(30);
  const usage = useAsync(() => api.usage(days), `usage|${days}`);
  const [adding, setAdding] = useState(false);

  return (
    <>
      <PageHeader
        title="AI"
        description="scraper0 uses AI to read new page layouts, posters and captchas. Use the shared provider or bring your own key."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setAdding(true)}>
              Add your own key
            </Button>
          </AdminOnly>
        }
      />

      <div className="space-y-6">
        <DataView state={providers} isEmpty={() => false}>
          {(data) => (
            <>
              <Card>
                <CardHeader title="Active provider" description="Used for this workspace's jobs right now." />
                <div className="px-4 py-4 sm:px-5">
                  {data.active ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{data.active.name}</span>
                      <Badge tone={data.active.scope === "tenant" ? "accent" : "neutral"}>
                        {data.active.scope === "tenant" ? "your key" : "shared"}
                      </Badge>
                      <span className="break-all font-mono text-xs text-muted">{data.active.model}</span>
                    </div>
                  ) : (
                    <p className="text-sm text-muted">
                      No provider is available. Jobs can still use learned layouts, but anything needing AI will fail.
                    </p>
                  )}
                </div>
              </Card>

              <Card>
                <CardHeader
                  title="Your providers"
                  description="Bring-your-own keys override the shared provider. Spend is still tracked against your budget."
                />
                <ProviderList
                  providers={data.own}
                  ops={canAdmin ? ops : undefined}
                  activeId={data.active?.id}
                  onChange={(own) => {
                    providers.setData({ ...data, own });
                    // The active provider may have changed (default / enabled flags).
                    providers.reload();
                  }}
                  emptyTitle="No keys of your own"
                  emptyDescription={
                    canAdmin
                      ? "Add a Groq, Anthropic or OpenAI-compatible key to use your own account."
                      : "A workspace admin can add your own provider key."
                  }
                  emptyAction={
                    canAdmin && (
                      <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
                        Add your own key
                      </Button>
                    )
                  }
                />
              </Card>

              <Card>
                <CardHeader title="Shared providers" description="Managed by the platform. Read-only." />
                <ProviderList
                  providers={data.shared}
                  activeId={data.active?.id}
                  emptyTitle="No shared providers"
                  emptyDescription="The platform admin hasn't configured a shared provider yet."
                />
              </Card>
            </>
          )}
        </DataView>

        <Card>
          <CardHeader
            title="Usage"
            actions={
              <>
                <label htmlFor="usage-days" className="sr-only">
                  Range
                </label>
                <Select id="usage-days" className="h-8 w-32" value={String(days)} onChange={(e) => setDays(Number(e.target.value))}>
                  <option value="7">Last 7 days</option>
                  <option value="30">Last 30 days</option>
                  <option value="90">Last 90 days</option>
                </Select>
              </>
            }
          />
          <DataView state={usage} isEmpty={() => false}>
            {(u) => (
              <div className="space-y-6 px-4 py-4 sm:px-5">
                <div className="grid gap-3 sm:grid-cols-3">
                  <Stat label={`Spend · ${days} days`} value={usd(u.total_usd, 2)} />
                  <Stat label="Monthly budget" value={u.budget_usd ? usd(u.budget_usd, 2) : "No cap"} />
                  <Stat
                    label="Calls"
                    value={u.by_day.reduce((s, d) => s + d.calls, 0).toLocaleString()}
                  />
                </div>
                <div>
                  <p className="mb-2 text-sm font-medium">This month vs budget</p>
                  <SpendThisMonth budget={u.budget_usd} />
                </div>
                <div>
                  <p className="text-sm font-medium">By day</p>
                  <DailySpendChart days={u.by_day} range={days} />
                </div>
                <div>
                  <p className="mb-3 text-sm font-medium">By purpose</p>
                  <BreakdownBars rows={u.by_purpose.map((p) => ({ label: p.purpose, usd: p.usd, calls: p.calls }))} />
                </div>
              </div>
            )}
          </DataView>
        </Card>
      </div>

      {adding && (
        <ProviderEditor
          provider={null}
          ops={ops}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            providers.reload();
          }}
        />
      )}
    </>
  );
}

/** Month-to-date spend from GET /v1/tenant (the usage window may not align with the month). */
function SpendThisMonth({ budget }: { budget: number | null }) {
  const tenant = useAsync(() => api.tenant(), "tenant");
  if (!tenant.data) return <p className="text-xs text-subtle">{tenant.error ? "Couldn't load spend." : "Loading…"}</p>;
  const t = tenant.data;
  const b = t.monthly_ai_budget_usd ?? budget;
  const shared = t.ai_shared_spend_this_month_usd ?? t.ai_spend_this_month_usd;
  const byo = Math.max(0, t.ai_spend_this_month_usd - shared);
  return (
    <div className="max-w-md">
      <p className="mb-1 text-sm tabular-nums">
        {usd(shared, 2)} <span className="text-muted">shared spend of {b ? usd(b, 2) : "no cap"}</span>
      </p>
      <BudgetBar spend={shared} budget={b} />
      <p className="mt-2 text-xs text-muted">
        The budget caps spend on shared providers. Spend on your own keys ({usd(byo, 2)} this month) isn&apos;t capped.
      </p>
    </div>
  );
}
