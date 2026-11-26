"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useState } from "react";
import { useSession } from "@/components/session";
import { ActionStatusBadge, KindBadge } from "@/components/sites";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Table,
  Td,
  Th,
  cx,
} from "@/components/ui";
import { ApiError, sitesApi, type Action, type Site } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import { ActionPanel, type RunMode } from "./action-panel";
import { AddActionModal } from "./add-action-modal";

function ActionsTable({
  actions,
  selected,
  onSelect,
}: {
  actions: Action[];
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Action</Th>
          <Th>Kind</Th>
          <Th className="hidden sm:table-cell">Implementation</Th>
          <Th>Status</Th>
          <Th className="hidden md:table-cell">Locked</Th>
          <Th className="hidden lg:table-cell">Last success</Th>
          <Th className="hidden lg:table-cell">Last failure</Th>
          <Th className="hidden md:table-cell">Failures</Th>
        </tr>
      </thead>
      <tbody>
        {actions.map((a) => {
          const active = a.name === selected;
          return (
            <tr key={a.name} className={cx(active ? "bg-accent-soft/40" : "hover:bg-surface-2/60")}>
              <Td>
                <button
                  type="button"
                  onClick={() => onSelect(a.name)}
                  aria-current={active ? "true" : undefined}
                  className="text-left font-mono font-medium hover:text-accent hover:underline"
                >
                  {a.name}
                </button>
                {a.entity && <p className="text-xs text-subtle">{a.entity}</p>}
              </Td>
              <Td>
                <KindBadge kind={a.kind} />
              </Td>
              <Td className="hidden text-muted sm:table-cell">{a.implementation}</Td>
              <Td>
                <ActionStatusBadge status={a.status} />
              </Td>
              <Td className="hidden font-mono md:table-cell">{a.locked_version !== null ? `v${a.locked_version}` : "—"}</Td>
              <Td className="hidden whitespace-nowrap text-muted lg:table-cell" >
                <span title={formatDate(a.last_success_at)}>{timeAgo(a.last_success_at)}</span>
              </Td>
              <Td className="hidden whitespace-nowrap text-muted lg:table-cell">
                <span title={formatDate(a.last_failure_at)}>{timeAgo(a.last_failure_at)}</span>
              </Td>
              <Td className={cx("hidden tabular-nums md:table-cell", a.consecutive_failures > 0 && "font-medium text-danger")}>
                {a.consecutive_failures}
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function SiteView({ siteId }: { siteId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { canAdmin, tenant } = useSession();
  const site = useAsync(() => sitesApi.get(siteId), `site|${siteId}`);

  const [addOpen, setAddOpen] = useState(() => params.get("add") === "1" && canAdmin);
  const [showExisting, setShowExisting] = useState(() => params.get("existing") === "1");
  const [autoRun, setAutoRun] = useState<{ name: string; mode: RunMode } | null>(null);

  const s = site.data;
  const requested = params.get("action");
  const selected = s ? (s.actions.find((a) => a.name === requested) ?? s.actions[0] ?? null) : null;

  const select = useCallback(
    (name: string, scroll = true) => {
      router.replace(`${pathname}?action=${encodeURIComponent(name)}`, { scroll: false });
      if (scroll) {
        requestAnimationFrame(() =>
          document.getElementById("action-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }),
        );
      }
    },
    [router, pathname],
  );

  const consumeAutoRun = useCallback(() => setAutoRun(null), []);

  if (!s) {
    const notOnboarded = site.error instanceof ApiError && site.error.code === "site_not_onboarded";
    return (
      <>
        <PageHeader title={siteId} back={{ href: "/app/sites", label: "Sites" }} />
        <Card>
          {notOnboarded ? (
            <EmptyState
              title="Site not onboarded"
              description={`No site with id "${siteId}" exists yet. Onboard it from the Sites page.`}
              action={
                <Link href="/app/sites" className="text-sm font-medium text-accent hover:underline">
                  Go to Sites
                </Link>
              }
            />
          ) : site.error ? (
            <ErrorState error={site.error} onRetry={site.reload} />
          ) : (
            <LoadingState label="Loading site…" />
          )}
        </Card>
      </>
    );
  }

  const broken = s.actions.filter((a) => a.status === "broken");
  const mine = !!s.created_by_tenant && !!tenant && [tenant.id, tenant.slug].includes(s.created_by_tenant);
  const createdBy = s.created_by_tenant ? (mine ? "your tenant" : s.created_by_tenant) : "the platform";

  function onCreated(a: Action, learnNow: boolean) {
    setAddOpen(false);
    site.setData((prev) => {
      const base = (prev ?? s) as Site;
      return { ...base, actions: [...base.actions.filter((x) => x.name !== a.name), a] };
    });
    site.reload();
    if (learnNow && a.implementation === "learned") setAutoRun({ name: a.name, mode: "learn" });
    select(a.name);
  }

  return (
    <>
      <PageHeader
        back={{ href: "/app/sites", label: "Sites" }}
        title={s.name || s.site_id}
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <a href={s.base_url} target="_blank" rel="noopener noreferrer" className="break-all text-accent hover:underline">
              {s.base_url}
            </a>
            <Badge className="font-mono">{s.site_id}</Badge>
            <span className="text-xs">
              Onboarded by {createdBy} · <span title={formatDate(s.created_at)}>{timeAgo(s.created_at)}</span>
            </span>
          </span>
        }
        actions={
          canAdmin && (
            <Button variant="primary" onClick={() => setAddOpen(true)}>
              Add action
            </Button>
          )
        }
      />

      <div className="space-y-6">
        {showExisting && (
          <Alert tone="accent" className="flex flex-wrap items-start justify-between gap-2">
            <span>
              {mine
                ? "You already onboarded this site. Its actions are below."
                : "Already onboarded by another tenant — reuse its actions or add your own."}
            </span>
            <button type="button" className="text-xs underline" onClick={() => setShowExisting(false)}>
              Dismiss
            </button>
          </Alert>
        )}

        {broken.map((a) => (
          <div
            key={a.name}
            role="alert"
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-danger/40 bg-danger-soft px-4 py-3 text-danger-soft-fg"
          >
            <div className="min-w-0 text-sm">
              <p className="font-semibold">
                Broken: <span className="font-mono">{a.name}</span>
              </p>
              <p>
                {a.consecutive_failures} consecutive failed run{a.consecutive_failures === 1 ? "" : "s"}
                {a.last_failure_at ? `, last ${timeAgo(a.last_failure_at)}` : ""}. Callers get stored items only until
                it is repaired and a new version is locked.
              </p>
            </div>
            {canAdmin && a.kind !== "write" ? (
              <Button
                variant="danger"
                onClick={() => {
                  setAutoRun({ name: a.name, mode: "repair" });
                  select(a.name);
                }}
              >
                Repair
              </Button>
            ) : (
              <Button onClick={() => select(a.name)}>View</Button>
            )}
          </div>
        ))}

        <Card>
          <CardHeader
            title="Actions"
            description="What scraper0 can do on this site. Reads are learned (or code); writes are code connectors."
          />
          {site.error ? (
            <div className="px-4 pt-3">
              <Alert>
                Couldn&apos;t refresh the site.{" "}
                <button className="underline" onClick={site.reload}>
                  Retry
                </button>
              </Alert>
            </div>
          ) : null}
          {s.actions.length === 0 ? (
            <EmptyState
              title="No actions yet"
              description={
                canAdmin
                  ? "Add a read action such as fetch_events. scraper0 learns it from the site and shows you the items."
                  : "A tenant admin can add an action, such as fetching the site's events."
              }
              action={
                canAdmin ? (
                  <Button variant="primary" onClick={() => setAddOpen(true)}>
                    Add action
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <ActionsTable actions={s.actions} selected={selected?.name ?? null} onSelect={select} />
          )}
        </Card>

        {selected && (
          <section id="action-panel" aria-label={`Action ${selected.name}`} className="scroll-mt-20">
            <ActionPanel
              key={selected.name}
              site={s}
              action={selected}
              onChanged={site.reload}
              autoRun={autoRun?.name === selected.name ? autoRun.mode : null}
              onAutoRunConsumed={consumeAutoRun}
            />
          </section>
        )}
      </div>

      {addOpen && <AddActionModal site={s} open onClose={() => setAddOpen(false)} onCreated={onCreated} />}
    </>
  );
}

export default function SitePage() {
  const { site_id } = useParams<{ site_id: string }>();
  return (
    <Suspense fallback={<LoadingState />}>
      <SiteView siteId={decodeURIComponent(site_id)} />
    </Suspense>
  );
}
