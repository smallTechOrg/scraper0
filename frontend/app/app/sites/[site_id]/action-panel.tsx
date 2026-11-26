"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useConfirm } from "@/components/confirm";
import { ItemsTable } from "@/components/items-table";
import { useSession } from "@/components/session";
import {
  ActionStatusBadge,
  isLookup,
  JobLink,
  JsonDetails,
  KindBadge,
  LiveIndicator,
  toItemRows,
} from "@/components/sites";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  CodeBlock,
  CopyButton,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingState,
  Stat,
  StatusBadge,
  Table,
  Td,
  Th,
} from "@/components/ui";
import {
  ApiError,
  errorMessage,
  sitesApi,
  type Action,
  type ActionContract,
  type ActionVersion,
  type Job,
  type ReadResult,
  type Site,
} from "@/lib/api";
import { formatDate, jobErrorText, timeAgo } from "@/lib/format";
import { useAsync, type AsyncState } from "@/lib/hooks";
import { isTerminal, useLiveJob } from "@/lib/live-job";

export type RunMode = "learn" | "repair";

interface RunState {
  jobId: string;
  version: number;
  mode: RunMode;
}

// The running learn/repair job survives a reload of the page (per tab).
function runKey(tenant: string, siteId: string, name: string) {
  return `s0_action_run:${tenant}:${siteId}:${name}`;
}
function loadRun(key: string): RunState | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<RunState>;
    return typeof v.jobId === "string" && typeof v.version === "number" && (v.mode === "learn" || v.mode === "repair")
      ? (v as RunState)
      : null;
  } catch {
    return null;
  }
}
function saveRun(key: string, run: RunState | null) {
  try {
    if (run) window.sessionStorage.setItem(key, JSON.stringify(run));
    else window.sessionStorage.removeItem(key);
  } catch {
    // storage unavailable — progress is still shown until the page is left
  }
}

function jobItems(job: Job): Array<Record<string, unknown>> | null {
  const r = job.result as { items?: unknown; preview?: { items?: unknown } } | null;
  if (r && Array.isArray(r.items)) return r.items as Array<Record<string, unknown>>;
  if (r?.preview && Array.isArray(r.preview.items)) return r.preview.items as Array<Record<string, unknown>>;
  return null;
}

// ---------------------------------------------------------------------------
// Live learn / repair progress
// ---------------------------------------------------------------------------

function JobProgress({
  run,
  onSucceeded,
  onDismiss,
  onRetry,
  canAdmin,
}: {
  run: RunState;
  onSucceeded: (job: Job) => void;
  onDismiss: () => void;
  onRetry: () => void;
  canAdmin: boolean;
}) {
  const { job, error, logs, mode } = useLiveJob(run.jobId);
  const done = useRef(false);
  const onSucceededRef = useRef(onSucceeded);
  useEffect(() => {
    onSucceededRef.current = onSucceeded;
  });

  useEffect(() => {
    if (job?.status === "succeeded" && !done.current) {
      done.current = true;
      onSucceededRef.current(job);
    }
  }, [job]);

  const verb = run.mode === "repair" ? "Repairing" : "Learning";
  const p = job?.progress ?? {};
  const err = job ? jobErrorText(job.error) : null;
  const failed = job && isTerminal(job) && job.status !== "succeeded";

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex flex-wrap items-center gap-2">
            {verb} version {run.version}
            {job && <StatusBadge status={job.status} />}
          </span>
        }
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <LiveIndicator mode={mode} />
            <JobLink id={run.jobId}>Job details</JobLink>
          </span>
        }
      />
      <div className="space-y-3 px-4 py-4 sm:px-5">
        {error && !job ? <ErrorState error={error} /> : null}
        {error && job ? <Alert tone="warning">Lost contact with the server; retrying…</Alert> : null}
        {!failed && (
          <>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
              <div className="h-full w-1/3 animate-pulse rounded-full bg-accent" />
            </div>
            <p className="text-sm">
              {p.message ??
                (job?.status === "queued"
                  ? "Waiting for a worker…"
                  : job?.status === "succeeded"
                    ? "Finished. Loading the preview…"
                    : "Rendering the site, finding structured data or its JSON API, and learning a recipe…")}
            </p>
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Pages" value={p.pages ?? 0} />
              <Stat label="Items" value={p.items ?? 0} />
              <Stat label="AI calls" value={p.llm_calls ?? 0} />
            </div>
            {logs.length > 0 && (
              <div className="rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-xs leading-relaxed" role="log" aria-live="polite">
                {logs.slice(-5).map((l) => (
                  <div key={l.id} className={l.level === "error" ? "text-danger" : ""}>
                    <span className="text-subtle">{new Date(l.at).toLocaleTimeString()}</span> {l.message}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        {failed && (
          <>
            <Alert>
              <p className="font-medium">
                {run.mode === "repair" ? "Repair" : "Learning"} {job.status === "cancelled" ? "was cancelled" : "failed"}
              </p>
              {err && <p className="mt-0.5">{err}</p>}
            </Alert>
            <div className="flex flex-wrap gap-2">
              {canAdmin && (
                <Button variant="primary" size="sm" onClick={onRetry}>
                  Try again
                </Button>
              )}
              <Button size="sm" onClick={onDismiss}>
                Dismiss
              </Button>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Draft review after learning
// ---------------------------------------------------------------------------

function DraftReview({
  versionNo,
  versions,
  fallbackItems,
  entity,
  canAdmin,
  onLock,
  onKeep,
}: {
  versionNo: number;
  versions: AsyncState<ActionVersion[]>;
  fallbackItems: Array<Record<string, unknown>> | null;
  entity: string | null;
  canAdmin: boolean;
  onLock: (v: number) => void;
  onKeep: () => void;
}) {
  const v = versions.data?.find((x) => x.version === versionNo);
  const items = v?.preview?.items ?? fallbackItems;
  const count = v?.preview?.count ?? items?.length ?? 0;
  const loading = !v && versions.loading;
  const isDraft = !v || v.status === "draft";

  return (
    <Card className="ring-2 ring-accent/30">
      <CardHeader
        title={
          <span className="flex flex-wrap items-center gap-2">
            Version {versionNo} preview
            {v && <ActionStatusBadge status={v.status} />}
          </span>
        }
        description={
          loading
            ? "Loading the preview…"
            : `${count} item${count === 1 ? "" : "s"} found${
                v?.preview?.checked_at ? ` · checked ${timeAgo(v.preview.checked_at)}` : ""
              }${items && count > items.length ? ` · showing the first ${items.length}` : ""}`
        }
        actions={
          <>
            <Button size="sm" onClick={onKeep}>
              {isDraft ? "Keep as draft" : "Close"}
            </Button>
            {canAdmin && isDraft && (
              <Button size="sm" variant="primary" onClick={() => onLock(versionNo)} disabled={count === 0}>
                Lock this version
              </Button>
            )}
          </>
        }
      />
      {loading ? (
        <LoadingState label="Loading preview…" />
      ) : !items || items.length === 0 ? (
        <EmptyState
          title="No items found"
          description="This version extracted nothing, so it can't be locked. Check the seed URLs and learn again, or open the job to see what happened."
        />
      ) : (
        <ItemsTable rows={toItemRows(items)} entity={entity ?? undefined} />
      )}
      {isDraft && count > 0 && (
        <p className="border-t border-border px-4 py-2.5 text-xs text-muted sm:px-5">
          A draft serves nobody. Lock it to make it the version callers use; drafts you keep stay listed under Versions.
        </p>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Versions
// ---------------------------------------------------------------------------

function VersionsCard({
  action,
  versions,
  canMutate,
  canLearn,
  onLearn,
  onLock,
  busy,
}: {
  action: Action;
  versions: AsyncState<ActionVersion[]>;
  canMutate: boolean;
  canLearn: boolean;
  onLearn: () => void;
  onLock: (v: number) => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const list = [...(versions.data ?? [])].sort((a, b) => b.version - a.version);

  return (
    <Card>
      <CardHeader
        title="Versions"
        description="Only the locked version serves callers. Locking a draft retires the previously locked one."
      />
      {versions.data === undefined ? (
        versions.error ? (
          <ErrorState error={versions.error} onRetry={versions.reload} />
        ) : (
          <LoadingState />
        )
      ) : list.length === 0 ? (
        <EmptyState
          title="No versions yet"
          description={
            canLearn
              ? "Learn the action from the live site to create a first draft, then check its items and lock it."
              : "Nothing has been learned for this action yet."
          }
          action={
            canLearn && canMutate ? (
              <Button variant="primary" onClick={onLearn} loading={busy}>
                Learn from the site
              </Button>
            ) : undefined
          }
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Version</Th>
              <Th>Status</Th>
              <Th className="hidden sm:table-cell">Created</Th>
              <Th className="hidden md:table-cell">Locked</Th>
              <Th>Items</Th>
              <Th className="w-0">
                <span className="sr-only">Actions</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {list.map((v) => {
              const expanded = open === v.version;
              return (
                <Fragment key={v.version}>
                  <tr className="hover:bg-surface-2/60">
                    <Td className="font-mono font-medium">
                      v{v.version}
                      {action.locked_version === v.version && <span className="sr-only"> (in use)</span>}
                    </Td>
                    <Td>
                      <ActionStatusBadge status={v.status} />
                    </Td>
                    <Td className="hidden whitespace-nowrap text-muted sm:table-cell">
                      <time dateTime={v.created_at} title={formatDate(v.created_at)}>
                        {timeAgo(v.created_at)}
                      </time>
                    </Td>
                    <Td className="hidden text-muted md:table-cell">
                      {v.locked_at ? (
                        <>
                          <time dateTime={v.locked_at} className="whitespace-nowrap" title={formatDate(v.locked_at)}>
                            {formatDate(v.locked_at)}
                          </time>
                          {v.locked_by && <span className="block text-xs text-subtle">by {v.locked_by}</span>}
                        </>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td className="tabular-nums">{v.preview ? v.preview.count : "—"}</Td>
                    <Td>
                      <div className="flex justify-end gap-1.5">
                        {v.preview && v.preview.items.length > 0 && (
                          <Button size="sm" variant="ghost" onClick={() => setOpen(expanded ? null : v.version)} aria-expanded={expanded}>
                            {expanded ? "Hide" : "Preview"}
                          </Button>
                        )}
                        {canMutate && v.status === "draft" && (
                          <Button size="sm" onClick={() => onLock(v.version)}>
                            Lock
                          </Button>
                        )}
                      </div>
                    </Td>
                  </tr>
                  {expanded && v.preview && (
                    <tr>
                      <td colSpan={6} className="border-b border-border bg-surface-2/50 p-0">
                        <ItemsTable rows={toItemRows(v.preview.items)} entity={action.entity ?? undefined} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Contract + Try it
// ---------------------------------------------------------------------------

function TryIt({ action }: { action: Action }) {
  const events = action.entity === "event";
  const [filter, setFilter] = useState("");
  const [includeOnline, setIncludeOnline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ReadResult | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const options = events
        ? { include_online: includeOnline, ...(filter.trim() ? { event_filter: filter.trim() } : {}) }
        : undefined;
      setResult(await sitesApi.read(action.site_id, action.name, options ? { options } : {}));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 border-t border-border px-4 py-4 sm:px-5">
      <div>
        <h3 className="text-sm font-semibold">Try it</h3>
        <p className="text-sm text-muted">
          Calls the read endpoint as your tenant with the default options. Stored items come back at once unless they
          are older than 48 hours; then it waits up to 60 s for a refresh.
        </p>
      </div>
      {events && (
        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <Field label="Event filter" optional hint="AI relevance filter, e.g. in-person civic volunteering">
            {(id, describedBy) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="in-person civic volunteering"
              />
            )}
          </Field>
          <div className="pb-2 sm:pb-6">
            <Checkbox label="Include online events" checked={includeOnline} onChange={setIncludeOnline} />
          </div>
        </div>
      )}
      <Button variant="primary" onClick={run} loading={busy}>
        {busy ? "Reading…" : "Run read"}
      </Button>
      {error && <Alert>{error}</Alert>}
      {result && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone={result.complete ? "success" : "warning"}>
              {result.complete ? "complete" : "incomplete: refresh still running"}
            </Badge>
            <Badge tone={result.health === "ok" ? "success" : "danger"}>health: {result.health}</Badge>
            <Badge>v{result.version}</Badge>
            <span className="text-muted">
              {result.items.length} item{result.items.length === 1 ? "" : "s"}
              {result.scraped_at ? ` · scraped ${timeAgo(result.scraped_at)}` : " · never scraped"}
            </span>
            {result.refresh_job_id && <JobLink id={result.refresh_job_id}>Refresh job</JobLink>}
          </div>
          {result.health === "broken" && (
            <Alert tone="warning">These are stored items: refreshes of this action are failing.</Alert>
          )}
          {result.ai_error && (
            <Alert tone="warning">
              <span className="font-medium">AI filter unavailable:</span> {result.ai_error}. Nothing was filtered out
              and categories are empty.
            </Alert>
          )}
          <div className="overflow-hidden rounded-md border border-border">
            {result.items.length === 0 ? (
              <EmptyState
                title="No items returned"
                description={
                  result.complete
                    ? "Nothing upcoming matched these options."
                    : "A refresh is still running. Run it again in a minute."
                }
              />
            ) : (
              <ItemsTable
                rows={result.items.map((it) => ({
                  key: it.id,
                  data: it.data,
                  meta: <time dateTime={it.updated_at}>{timeAgo(it.updated_at)}</time>,
                }))}
                entity={action.entity ?? undefined}
                metaHeader="Updated"
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function ContractCard({ action, contract }: { action: Action; contract: AsyncState<ActionContract> }) {
  const notAvailable = contract.error instanceof ApiError && contract.error.code === "action_not_available";
  const c = notAvailable ? undefined : contract.data;
  const lookup = isLookup(action);

  return (
    <Card>
      <CardHeader
        title="Contract"
        description="What callers use: the locked version of this action, its schemas and a ready-to-run curl."
      />
      {notAvailable || (contract.data === undefined && !contract.loading && !contract.error) ? (
        <EmptyState
          title="No locked version yet"
          description="Learn the action, check the preview and lock a version. The contract and curl appear here."
        />
      ) : c === undefined ? (
        contract.error ? (
          <ErrorState error={contract.error} onRetry={contract.reload} />
        ) : (
          <LoadingState label="Loading contract…" />
        )
      ) : (
        <>
          <div className="space-y-4 px-4 py-4 sm:px-5">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
              <div className="min-w-0">
                <dt className="text-xs text-subtle">Site id</dt>
                <dd className="break-all font-mono">{c.site_id}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-xs text-subtle">Action</dt>
                <dd className="break-all font-mono">{c.action}</dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Kind</dt>
                <dd>
                  <KindBadge kind={c.kind} />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Version</dt>
                <dd className="font-mono">v{c.version}</dd>
              </div>
            </dl>
            <div>
              <p className="mb-1 text-xs text-subtle">Endpoint</p>
              <div className="flex flex-wrap items-center gap-2">
                <code className="min-w-0 flex-1 break-all rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-xs">
                  {c.endpoint}
                </code>
                <CopyButton value={c.endpoint} />
              </div>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              <JsonDetails label="Input schema" value={c.input_schema} />
              <JsonDetails label="Output schema" value={c.output_schema} />
            </div>
            <div>
              <p className="mb-1 text-xs text-subtle">Test it with curl (replace the API key placeholder with one of your keys)</p>
              <CodeBlock code={c.curl} label="curl command" />
            </div>
          </div>
          {action.kind === "read" && !lookup && <TryIt action={action} />}
          {action.kind === "read" && lookup && (
            <p className="border-t border-border px-4 py-3 text-sm text-muted sm:px-5">
              This is a lookup: it needs an input (and usually site credentials), so run it with the curl above.
            </p>
          )}
        </>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

export function ActionPanel({
  site,
  action,
  onChanged,
  autoRun,
  onAutoRunConsumed,
}: {
  site: Site;
  action: Action;
  /** Reload the site (action status / locked version changed). */
  onChanged: () => void;
  /** Start learning or repairing as soon as the panel opens. */
  autoRun: RunMode | null;
  onAutoRunConsumed: () => void;
}) {
  const { canAdmin, tenant } = useSession();
  const confirm = useConfirm();
  const storageKey = runKey(tenant?.id ?? "", site.site_id, action.name);
  const [run, setRunState] = useState<RunState | null>(() => (typeof window === "undefined" ? null : loadRun(storageKey)));
  const [review, setReview] = useState<{ version: number; items: Array<Record<string, unknown>> | null } | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const write = action.kind === "write";
  const learned = action.implementation === "learned";
  const broken = action.status === "broken";
  const canMutate = canAdmin && !write;
  const canLearn = learned && !write;

  const setRun = useCallback(
    (r: RunState | null) => {
      saveRun(storageKey, r);
      setRunState(r);
    },
    [storageKey],
  );

  const versions = useAsync(() => sitesApi.versions(site.site_id, action.name), `versions|${site.site_id}|${action.name}`);
  const contract = useAsync(
    () => sitesApi.contract(site.site_id, action.name),
    `contract|${site.site_id}|${action.name}|${action.locked_version ?? "none"}`,
  );

  const reloadVersions = versions.reload;
  const start = useCallback(
    async (mode: RunMode) => {
      setStarting(true);
      setStartError(null);
      setReview(null);
      try {
        const res =
          mode === "repair"
            ? await sitesApi.repair(site.site_id, action.name)
            : await sitesApi.learn(site.site_id, action.name);
        setRun({ jobId: res.job_id, version: res.version, mode });
        reloadVersions();
      } catch (err) {
        setStartError(errorMessage(err));
      } finally {
        setStarting(false);
      }
    },
    [site.site_id, action.name, setRun, reloadVersions],
  );

  // Auto-start (from "Add action → learn now" or the broken banner's Repair).
  const autoStarted = useRef(false);
  useEffect(() => {
    if (!autoRun) {
      autoStarted.current = false;
      return;
    }
    if (autoStarted.current) return;
    autoStarted.current = true;
    onAutoRunConsumed();
    if (canMutate && !run) void start(autoRun);
  }, [autoRun, canMutate, run, start, onAutoRunConsumed]);

  function onJobSucceeded(job: Job) {
    if (!run) return;
    setReview({ version: run.version, items: jobItems(job) });
    setRun(null);
    versions.reload();
    onChanged();
  }

  async function lock(v: number) {
    const current = action.locked_version;
    const out: { contract?: ActionContract } = {};
    const ok = await confirm({
      title: `Lock version ${v}?`,
      message: (
        <>
          <span className="block">
            Callers of <span className="font-mono text-fg">{site.site_id}</span> ·{" "}
            <span className="font-mono text-fg">{action.name}</span> will be served by version {v} from now on. Every
            tenant shares this site, so this affects all of them.
          </span>
          {current !== null && current !== v && (
            <span className="mt-2 block">Version {current}, the one in use now, becomes retired.</span>
          )}
        </>
      ),
      confirmLabel: "Lock version",
      danger: false,
      action: async () => {
        out.contract = await sitesApi.lock(site.site_id, action.name, v);
      },
    });
    if (!ok) return;
    setReview(null);
    if (out.contract) contract.setData(out.contract);
    versions.reload();
    onChanged();
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title={
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-mono">{action.name}</span>
              <KindBadge kind={action.kind} />
              <Badge>{action.implementation}</Badge>
              <ActionStatusBadge status={action.status} />
            </span>
          }
          description={action.entity ? `Entity: ${action.entity}` : undefined}
          actions={
            canMutate && !run ? (
              <>
                {canLearn && (
                  <Button onClick={() => start("learn")} loading={starting} disabled={starting}>
                    {(versions.data?.length ?? 0) > 0 ? "Learn again" : "Learn"}
                  </Button>
                )}
                {broken && (
                  <Button variant="danger" onClick={() => start("repair")} loading={starting} disabled={starting}>
                    Repair
                  </Button>
                )}
              </>
            ) : undefined
          }
        />
        <div className="space-y-4 px-4 py-4 sm:px-5">
          {broken && (
            <Alert>
              <p className="font-medium">Broken</p>
              <p className="mt-0.5">
                The last {action.consecutive_failures} run{action.consecutive_failures === 1 ? "" : "s"} failed.
                {action.locked_version !== null &&
                  ` Callers still get the stored items from version ${action.locked_version}, with health "broken".`}{" "}
                {canMutate
                  ? learned
                    ? "Repair re-learns it from the live site and creates a new draft; lock it to take effect."
                    : "This is a code action: once the connector is fixed, run Repair and lock the new draft."
                  : "A tenant admin can repair it."}
              </p>
            </Alert>
          )}
          {write && (
            <Alert tone="accent">
              Write actions are code connectors, reviewed and tested by the scraper0 team. They are read-only here:
              callers run them with the contract below.
            </Alert>
          )}
          {startError && <Alert>{startError}</Alert>}
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm md:grid-cols-4">
            <div>
              <dt className="text-xs text-subtle">Locked version</dt>
              <dd className="font-mono">{action.locked_version !== null ? `v${action.locked_version}` : "none"}</dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Last success</dt>
              <dd title={formatDate(action.last_success_at)}>{timeAgo(action.last_success_at)}</dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Last failure</dt>
              <dd title={formatDate(action.last_failure_at)}>{timeAgo(action.last_failure_at)}</dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Consecutive failures</dt>
              <dd className={action.consecutive_failures > 0 ? "font-medium text-danger tabular-nums" : "tabular-nums"}>
                {action.consecutive_failures}
              </dd>
            </div>
          </dl>
          {action.seed_urls?.length > 0 && (
            <div>
              <p className="text-xs text-subtle">Seed URLs</p>
              <ul className="mt-0.5 space-y-0.5 text-sm">
                {action.seed_urls.map((u) => (
                  <li key={u} className="break-all">
                    <a href={u} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                      {u}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Card>

      {run && (
        <JobProgress
          key={run.jobId}
          run={run}
          canAdmin={canMutate}
          onSucceeded={onJobSucceeded}
          onDismiss={() => {
            setRun(null);
            versions.reload();
          }}
          onRetry={() => {
            const mode = run.mode;
            setRun(null);
            void start(mode);
          }}
        />
      )}

      {review && (
        <DraftReview
          versionNo={review.version}
          versions={versions}
          fallbackItems={review.items}
          entity={action.entity}
          canAdmin={canMutate}
          onLock={lock}
          onKeep={() => setReview(null)}
        />
      )}

      <VersionsCard
        action={action}
        versions={versions}
        canMutate={canMutate}
        canLearn={canLearn && !run}
        onLearn={() => start("learn")}
        onLock={lock}
        busy={starting}
      />

      <ContractCard action={action} contract={contract} />
    </div>
  );
}
