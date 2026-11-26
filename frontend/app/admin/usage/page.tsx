"use client";

import { useState } from "react";
import { BreakdownBars, DailySpendChart } from "@/components/charts";
import { Card, CardHeader, DataView, PageHeader, Select, Stat } from "@/components/ui";
import { api } from "@/lib/api";
import { usd } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

export default function AdminUsagePage() {
  const [days, setDays] = useState(30);
  const usage = useAsync(() => api.admin.usage(days), `admin-usage|${days}`);

  return (
    <>
      <PageHeader
        title="Usage"
        description="AI spend across all tenants."
        actions={
          <>
            <label htmlFor="admin-usage-days" className="sr-only">
              Range
            </label>
            <Select id="admin-usage-days" className="h-9 w-36" value={String(days)} onChange={(e) => setDays(Number(e.target.value))}>
              <option value="7">Last 7 days</option>
              <option value="30">Last 30 days</option>
              <option value="90">Last 90 days</option>
            </Select>
          </>
        }
      />
      <DataView state={usage} isEmpty={() => false}>
        {(u) => (
          <div className="space-y-6">
            <div className="grid gap-3 sm:grid-cols-3">
              <Stat label={`Total · ${days} days`} value={usd(u.total_usd, 2)} />
              <Stat label="Tenants with usage" value={u.by_tenant.filter((t) => t.usd > 0 || t.calls > 0).length} />
              <Stat label="Calls" value={u.by_tenant.reduce((s, t) => s + t.calls, 0).toLocaleString()} />
            </div>
            <Card>
              <CardHeader title="By day" />
              <div className="px-4 py-4 sm:px-5">
                <DailySpendChart days={u.by_day} range={days} />
              </div>
            </Card>
            <Card>
              <CardHeader title="By tenant" />
              <div className="px-4 py-4 sm:px-5">
                <BreakdownBars rows={u.by_tenant.map((t) => ({ label: t.slug, usd: t.usd, calls: t.calls }))} />
              </div>
            </Card>
          </div>
        )}
      </DataView>
    </>
  );
}
