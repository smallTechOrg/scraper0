"use client";

import Link from "next/link";
import { JobsTable } from "@/components/jobs";
import { useSession } from "@/components/session";
import {
  BudgetBar,
  ButtonLink,
  Card,
  CardHeader,
  DataView,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
} from "@/components/ui";
import { api } from "@/lib/api";
import { usd } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

export default function OverviewPage() {
  const { tenant, user } = useSession();
  const jobs = useAsync(() => api.jobs({ limit: 8 }), "recent-jobs");
  const info = useAsync(() => api.tenant(), "tenant");

  return (
    <>
      <PageHeader
        title={`Welcome${user.name ? `, ${user.name.split(" ")[0]}` : ""}`}
        description={`Here's what's happening in ${tenant?.name ?? "your workspace"}.`}
        actions={
          <ButtonLink href="/app/sites" variant="primary">
            Onboard a site
          </ButtonLink>
        }
      />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <CardHeader
              title="Recent jobs"
              actions={
                <Link href="/app/jobs" className="text-sm font-medium text-accent hover:underline">
                  View all
                </Link>
              }
            />
            <DataView
              state={jobs}
              empty={
                <EmptyState
                  title="No jobs yet"
                  description="Onboard a site, let scraper0 learn an action and lock it. Learning, refreshes and reads show up here."
                  action={
                    <ButtonLink href="/app/sites" variant="primary" size="sm">
                      Onboard a site
                    </ButtonLink>
                  }
                />
              }
            >
              {(list) => <JobsTable jobs={list} compact />}
            </DataView>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader title="AI spend this month" description="Shared-provider spend against your monthly budget." />
            <div className="p-4 sm:p-5">
              {info.error && !info.data ? (
                <ErrorState error={info.error} onRetry={info.reload} />
              ) : !info.data ? (
                <LoadingState />
              ) : (
                <>
                  <p className="text-2xl font-semibold tabular-nums">
                    {usd(info.data.ai_shared_spend_this_month_usd ?? info.data.ai_spend_this_month_usd, 2)}
                    <span className="ml-1 text-sm font-normal text-muted">
                      / {info.data.monthly_ai_budget_usd ? usd(info.data.monthly_ai_budget_usd, 2) : "no cap"}
                    </span>
                  </p>
                  <div className="mt-3">
                    <BudgetBar
                      spend={info.data.ai_shared_spend_this_month_usd ?? info.data.ai_spend_this_month_usd}
                      budget={info.data.monthly_ai_budget_usd}
                    />
                  </div>
                  {info.data.ai_shared_spend_this_month_usd !== undefined &&
                    info.data.ai_spend_this_month_usd > info.data.ai_shared_spend_this_month_usd && (
                      <p className="mt-2 text-xs text-muted">
                        Plus {usd(info.data.ai_spend_this_month_usd - info.data.ai_shared_spend_this_month_usd, 2)} on your own
                        keys (not capped).
                      </p>
                    )}
                  <Link href="/app/ai" className="mt-4 inline-block text-sm font-medium text-accent hover:underline">
                    Usage details
                  </Link>
                </>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader title="Quick actions" />
            <ul className="divide-y divide-border">
              {[
                { href: "/app/sites", label: "Onboard a site", hint: "Learn an action, check it, lock it" },
                { href: "/app/sources", label: "Schedule a refresh", hint: "Keep a site action's items fresh" },
                { href: "/app/items", label: "Browse items", hint: "Stored results and change feed" },
              ].map((a) => (
                <li key={a.href}>
                  <Link href={a.href} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-surface-2 sm:px-5">
                    <span>
                      <span className="block text-sm font-medium">{a.label}</span>
                      <span className="block text-xs text-subtle">{a.hint}</span>
                    </span>
                    <span aria-hidden className="text-subtle">
                      →
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </>
  );
}
