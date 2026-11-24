"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useConfirm } from "@/components/confirm";
import { ItemsTable } from "@/components/items-table";
import { describeJob } from "@/components/jobs";
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
  Spinner,
  Stat,
  StatusBadge,
} from "@/components/ui";
import { api, type ConnectorResult, type CrawlResult, type Job } from "@/lib/api";
import { cellText, duration, formatDate, jobErrorText, usd } from "@/lib/format";
import { isTerminal, useLiveJob, type LiveMode } from "@/lib/live-job";

function LiveIndicator({ mode }: { mode: LiveMode }) {
  const label = {
    connecting: "Connecting…",
    live: "Live",
    polling: "Updating every few seconds",
    done: "Finished",
  }[mode];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" aria-live="polite">
      {mode === "live" && <span className="size-2 animate-pulse rounded-full bg-success" aria-hidden />}
      {(mode === "connecting" || mode === "polling") && <Spinner className="size-3" />}
      {label}
    </span>
  );
}

function asCrawlResult(job: Job): CrawlResult | null {
  if (job.kind === "connector") return null;
  const r = job.result as Partial<CrawlResult> | null;
  if (!r || typeof r !== "object" || !Array.isArray(r.items)) return null;
  return { ...r, items: r.items, stats: r.stats ?? {} };
}

function connectorResult(job: Job): ConnectorResult | null {
  if (job.kind !== "connector" || !job.result || typeof job.result !== "object") return null;
  const r = job.result as Partial<ConnectorResult>;
  // Tolerate a bare payload (older shape) by treating the whole result as data.
  return "data" in r ? (r as ConnectorResult) : { data: r };
}

/** Connector payloads that contain a list of events or items get a table too. */
function connectorRows(payload: unknown): Array<Record<string, unknown>> | null {
  if (!payload || typeof payload !== "object") return null;
  const r = payload as Record<string, unknown>;
  for (const k of ["events", "items"]) {
    if (Array.isArray(r[k]) && (r[k] as unknown[]).every((x) => x && typeof x === "object")) {
      return r[k] as Array<Record<string, unknown>>;
    }
  }
  return null;
}

function AiErrors({ errors }: { errors?: string[] }) {
  if (!errors?.length) return null;
  const unique = [...new Set(errors)];
  return (
    <Alert tone="warning">
      <p className="font-medium">AI was unavailable for part of this job</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {unique.slice(0, 5).map((e) => (
          <li key={e}>{e}</li>
        ))}
        {unique.length > 5 && <li>…and {unique.length - 5} more</li>}
      </ul>
      <p className="mt-1 text-xs">
        Check the AI page: a provider must be configured and the monthly budget not reached.{" "}
        <Link href="/app/ai" className="underline">
          Open AI settings
        </Link>
      </p>
    </Alert>
  );
}

function inputEntity(job: Job): string | undefined {
  const i = job.input as { entity?: unknown } | null;
  if (!i) return job.kind === "connector" ? "event" : undefined;
  if (typeof i.entity === "string") return i.entity;
  if (i.entity && typeof i.entity === "object") return (i.entity as { name?: string }).name;
  return job.kind === "ingest" ? "event" : undefined;
}

function Logs({ lines }: { lines: { id: number; at: string; level?: string; message: string }[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);
  if (lines.length === 0) return <p className="px-4 py-6 text-center text-sm text-subtle">No log lines yet.</p>;
  return (
    <div ref={ref} className="max-h-72 overflow-y-auto px-4 py-3 font-mono text-xs leading-relaxed" role="log" aria-live="polite">
      {lines.map((l) => (
        <div key={l.id} className={l.level === "error" ? "text-danger" : l.level === "warning" ? "text-warning-soft-fg" : ""}>
          <span className="text-subtle">{new Date(l.at).toLocaleTimeString()}</span> {l.message}
        </div>
      ))}
    </div>
  );
}

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { job, setJob, error, logs, mode } = useLiveJob(id);
  const confirm = useConfirm();
  const [showInput, setShowInput] = useState(false);

  if (!job) {
    return (
      <>
        <PageHeader title="Job" back={{ href: "/app/jobs", label: "Jobs" }} />
        <Card>{error ? <ErrorState error={error} /> : <LoadingState label="Loading job…" />}</Card>
      </>
    );
  }

  const crawl = asCrawlResult(job);
  const conn = connectorResult(job);
  const connRows = conn ? connectorRows(conn.data) : null;
  const ai = crawl?.ai ?? conn?.ai;
  const stored = crawl?.stored ?? conn?.stored;
  const p = job.progress ?? {};
  const err = jobErrorText(job.error);
  const active = !isTerminal(job);
  const entity = crawl?.entity ?? inputEntity(job);

  async function onCancel() {
    await confirm({
      title: "Cancel this job?",
      message: "Running jobs stop at the next page. Items already extracted are kept.",
      confirmLabel: "Cancel job",
      action: async () => {
        const j = await api.cancelJob(job!.id);
        setJob(j);
      },
    });
  }

  return (
    <>
      <PageHeader
        back={{ href: "/app/jobs", label: "Jobs" }}
        title={
          <span className="flex flex-wrap items-center gap-3">
            <span className="break-all">{describeJob(job)}</span>
            <StatusBadge status={job.status} />
          </span>
        }
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Badge>{job.kind}</Badge>
            <span className="font-mono text-xs">{job.id}</span>
            {job.source_id && (
              <Link href="/app/sources" className="text-xs text-accent hover:underline">
                from a scheduled source
              </Link>
            )}
            <LiveIndicator mode={mode} />
          </span>
        }
        actions={
          active && (
            <Button variant="danger" onClick={onCancel}>
              Cancel job
            </Button>
          )
        }
      />

      {error && <Alert tone="warning" className="mb-4">Lost contact with the server; retrying…</Alert>}

      <div className="space-y-6">
        {/* Progress */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Pages" value={p.pages ?? 0} />
          <Stat label="Items" value={p.items ?? crawl?.items.length ?? 0} />
          <Stat label="AI calls" value={p.llm_calls ?? 0} />
          <Stat label="Layouts learned" value={p.recipes_learned ?? 0} />
        </div>

        <AiErrors errors={ai?.errors} />

        <Card>
          <CardHeader title="Progress" />
          <div className="space-y-3 px-4 py-4 sm:px-5">
            {active && (
              <div className="h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden>
                <div className="h-full w-1/3 animate-pulse rounded-full bg-accent" />
              </div>
            )}
            <p className="text-sm">
              {p.message ??
                (job.status === "queued"
                  ? "Waiting for a worker…"
                  : job.status === "succeeded"
                    ? "Finished."
                    : job.status === "cancelled"
                      ? "Cancelled."
                      : job.status === "failed"
                        ? "Failed."
                        : "Working…")}
            </p>
            <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-muted sm:grid-cols-3">
              <div>
                <dt className="inline text-subtle">Created </dt>
                <dd className="inline">{formatDate(job.created_at)}</dd>
              </div>
              <div>
                <dt className="inline text-subtle">Started </dt>
                <dd className="inline">{formatDate(job.started_at)}</dd>
              </div>
              <div>
                <dt className="inline text-subtle">Duration </dt>
                <dd className="inline">{duration(job.started_at, job.finished_at)}</dd>
              </div>
            </dl>
            {err && <Alert>{err}</Alert>}
          </div>
        </Card>

        {/* Result */}
        {crawl && (
          <>
            {(stored || ai || Object.keys(crawl.stats).length > 0) && (
              <Card>
                <CardHeader title="Stats" />
                <dl className="grid grid-cols-2 gap-x-6 gap-y-3 px-4 py-4 text-sm sm:grid-cols-4 sm:px-5">
                  {stored &&
                    (["created", "updated", "unchanged"] as const).map((k) => (
                      <div key={k}>
                        <dt className="text-xs text-subtle">Stored · {k}</dt>
                        <dd className="font-medium tabular-nums">{stored[k] ?? 0}</dd>
                      </div>
                    ))}
                  {ai && (
                    <>
                      <div>
                        <dt className="text-xs text-subtle">AI calls</dt>
                        <dd className="font-medium tabular-nums">{ai.calls ?? 0}</dd>
                      </div>
                      <div>
                        <dt className="text-xs text-subtle">AI cost</dt>
                        <dd className="font-medium tabular-nums">{usd(ai.cost_usd ?? 0)}</dd>
                      </div>
                    </>
                  )}
                  {crawl.needs_review !== undefined && (
                    <div>
                      <dt className="text-xs text-subtle">Needs review</dt>
                      <dd className="font-medium tabular-nums">{crawl.needs_review}</dd>
                    </div>
                  )}
                  {Object.entries(crawl.stats).map(([k, v]) => (
                    <div key={k}>
                      <dt className="text-xs text-subtle">{k.replace(/_/g, " ")}</dt>
                      <dd className="break-words font-medium tabular-nums">{cellText(v)}</dd>
                    </div>
                  ))}
                </dl>
              </Card>
            )}
            {crawl.report && crawl.report.length > 0 && (
              <Card>
                <CardHeader title="Inputs" description="What was read from each file, text or URL." />
                <ul className="divide-y divide-border">
                  {crawl.report.map((r, i) => (
                    <li key={i} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm sm:px-5">
                      <span className="min-w-0 break-all">{r.input}</span>
                      {r.error ? (
                        <Badge tone="danger">{r.error}</Badge>
                      ) : (
                        <Badge tone={r.events > 0 ? "success" : "neutral"}>
                          {r.events} item{r.events === 1 ? "" : "s"}
                        </Badge>
                      )}
                    </li>
                  ))}
                </ul>
              </Card>
            )}
            <Card>
              <CardHeader
                title={`Items (${crawl.items.length})`}
                description={entity ? `Entity: ${entity}` : undefined}
                actions={
                  crawl.items.length > 0 && (
                    <Link href={`/app/items${entity ? `?entity=${encodeURIComponent(entity)}` : ""}`} className="text-sm font-medium text-accent hover:underline">
                      Browse stored items
                    </Link>
                  )
                }
              />
              {crawl.items.length === 0 ? (
                <EmptyState title="No items extracted" description="Check the log below or try different URLs or options." />
              ) : (
                <ItemsTable
                  entity={entity}
                  rows={crawl.items.map((d, i) => ({ key: String(d.link ?? d.url ?? i) + ":" + i, data: d }))}
                />
              )}
            </Card>
          </>
        )}

        {conn && (
          <Card>
            <CardHeader
              title="Connector result"
              description={
                stored || ai
                  ? [
                      stored && `Stored: ${stored.created} new, ${stored.updated} updated, ${stored.unchanged} unchanged`,
                      ai && `AI: ${ai.calls ?? 0} calls, ${usd(ai.cost_usd ?? 0)}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : undefined
              }
            />
            {connRows && connRows.length > 0 && (
              <ItemsTable rows={connRows.map((d, i) => ({ key: String(i), data: d }))} />
            )}
            <pre className="max-h-96 overflow-auto border-t border-border bg-surface-2 px-4 py-3 font-mono text-xs leading-relaxed">
              {JSON.stringify(conn.data, null, 2)}
            </pre>
          </Card>
        )}

        {!crawl && job.kind !== "connector" && job.status === "succeeded" && job.result != null && (
          <Card>
            <CardHeader title="Result" />
            <pre className="max-h-96 overflow-auto px-4 py-3 font-mono text-xs">{JSON.stringify(job.result, null, 2)}</pre>
          </Card>
        )}

        <Card>
          <CardHeader title="Log" description={mode === "live" ? "Streaming" : undefined} />
          <Logs lines={logs} />
        </Card>

        <Card>
          <CardHeader
            title="Input"
            actions={
              <Button size="sm" variant="ghost" onClick={() => setShowInput((s) => !s)} aria-expanded={showInput}>
                {showInput ? "Hide" : "Show"}
              </Button>
            }
          />
          {showInput && (
            <pre className="max-h-96 overflow-auto px-4 py-3 font-mono text-xs">{JSON.stringify(job.input, null, 2)}</pre>
          )}
        </Card>
      </div>
    </>
  );
}
