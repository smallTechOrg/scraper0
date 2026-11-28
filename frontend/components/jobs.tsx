"use client";

import Link from "next/link";
import type { Job, JobSummary } from "@/lib/api";
import { duration, hostname, timeAgo } from "@/lib/format";
import { Badge, StatusBadge, Table, Td, Th } from "./ui";

/** One-line description of what a job is doing, from its input. */
export function describeJob(job: Pick<Job, "kind"> & { input?: unknown }): string {
  const input = (job.input ?? null) as Record<string, unknown> | null;
  if (!input || typeof input !== "object") return job.kind;
  if (job.kind.startsWith("site_")) {
    const verbs: Record<string, string> = { site_refresh: "refresh", site_learn: "learn", site_write: "write" };
    const verb = verbs[job.kind] ?? job.kind;
    return `${String(input.site_id ?? "site")} · ${String(input.action ?? "")} · ${verb}`;
  }
  if (job.kind === "connector" && input.site_id) {
    return `${String(input.site_id)} · ${String(input.site_action ?? input.action ?? "")} · lookup`;
  }
  if (job.kind === "crawl") {
    const entity = typeof input.entity === "string" ? input.entity : (input.entity as { name?: string })?.name;
    const urls = Array.isArray(input.urls) ? (input.urls as string[]) : [];
    const first = urls[0] ? hostname(urls[0]) : "";
    return `${entity ?? "crawl"}${first ? ` · ${first}` : ""}${urls.length > 1 ? ` +${urls.length - 1}` : ""}`;
  }
  if (job.kind === "connector") {
    return `${String(input.connector ?? "connector")} · ${String(input.action ?? "")}`;
  }
  if (job.kind === "ingest") {
    return `${typeof input.entity === "string" ? input.entity : "event"} · upload`;
  }
  return job.kind;
}

export function JobsTable({ jobs, compact = false }: { jobs: JobSummary[]; compact?: boolean }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Job</Th>
          <Th>Status</Th>
          {!compact && <Th>Progress</Th>}
          <Th>Created</Th>
          {!compact && <Th>Duration</Th>}
        </tr>
      </thead>
      <tbody>
        {jobs.map((j) => (
          <tr key={j.id} className="hover:bg-surface-2/60">
            <Td>
              <Link href={`/app/jobs/${encodeURIComponent(j.id)}`} className="font-medium hover:text-accent hover:underline">
                {describeJob(j)}
              </Link>
              <div className="mt-0.5 flex items-center gap-1.5 text-xs text-subtle">
                <Badge>{j.kind}</Badge>
                {j.site_id && (
                  <Link href={`/app/sites/${encodeURIComponent(j.site_id)}${j.action ? `?action=${encodeURIComponent(j.action)}` : ""}`}
                        className="hover:text-accent hover:underline">
                    {j.site_id}
                  </Link>
                )}
                {j.source_id && <span>scheduled</span>}
                <span className="font-mono">{j.id.slice(0, 10)}</span>
              </div>
            </Td>
            <Td>
              <StatusBadge status={j.status} />
            </Td>
            {!compact && (
              <Td className="max-w-[18rem] text-xs text-muted">
                <span className="line-clamp-2">
                  {j.progress?.message ??
                    (j.progress?.items !== undefined ? `${j.progress.items} items` : "—")}
                </span>
              </Td>
            )}
            <Td className="whitespace-nowrap text-muted" >
              <time dateTime={j.created_at} title={j.created_at}>
                {timeAgo(j.created_at)}
              </time>
            </Td>
            {!compact && (
              <Td className="whitespace-nowrap tabular-nums text-muted">{duration(j.started_at, j.finished_at)}</Td>
            )}
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
