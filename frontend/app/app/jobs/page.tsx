"use client";

import { useState } from "react";
import { JobsTable } from "@/components/jobs";
import { Alert, Button, ButtonLink, Card, DataView, EmptyState, PageHeader, Select } from "@/components/ui";
import { api, errorMessage, JOB_STATUSES, type JobSummary } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

const PAGE = 50;

export default function JobsPage() {
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [more, setMore] = useState<{ busy: boolean; error: string | null; done: boolean }>({
    busy: false,
    error: null,
    done: false,
  });

  const jobs = useAsync(async () => {
    setMore({ busy: false, error: null, done: false });
    const list = await api.jobs({ status, kind, limit: PAGE });
    if (list.length < PAGE) setMore((m) => ({ ...m, done: true }));
    return list;
  }, `${status}|${kind}`);

  async function loadMore() {
    const last = jobs.data?.[jobs.data.length - 1];
    if (!last) return;
    setMore({ busy: true, error: null, done: false });
    try {
      const next = await api.jobs({ status, kind, limit: PAGE, before: last.created_at });
      jobs.setData((prev: JobSummary[] | undefined) => {
        const seen = new Set((prev ?? []).map((j) => j.id));
        return [...(prev ?? []), ...next.filter((j) => !seen.has(j.id))];
      });
      setMore({ busy: false, error: null, done: next.length < PAGE });
    } catch (err) {
      setMore({ busy: false, error: errorMessage(err), done: false });
    }
  }

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every crawl, upload and connector run is a job."
        actions={
          <ButtonLink href="/app/jobs/new" variant="primary">
            New job
          </ButtonLink>
        }
      />
      <Card>
        <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3 sm:px-5">
          <div>
            <label htmlFor="status-filter" className="mb-1 block text-xs font-medium text-subtle">
              Status
            </label>
            <Select id="status-filter" value={status} onChange={(e) => setStatus(e.target.value)} className="h-9 w-40">
              <option value="">All statuses</option>
              {JOB_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <label htmlFor="kind-filter" className="mb-1 block text-xs font-medium text-subtle">
              Kind
            </label>
            <Select id="kind-filter" value={kind} onChange={(e) => setKind(e.target.value)} className="h-9 w-40">
              <option value="">All kinds</option>
              <option value="crawl">crawl</option>
              <option value="connector">connector</option>
              <option value="ingest">ingest</option>
            </Select>
          </div>
          <Button size="sm" variant="ghost" className="ml-auto" onClick={jobs.reload} loading={jobs.loading}>
            Refresh
          </Button>
        </div>
        <DataView
          state={jobs}
          empty={
            <EmptyState
              title={status || kind ? "No jobs match these filters" : "No jobs yet"}
              description={status || kind ? "Try a different status or kind." : "Create a job to start extracting data."}
              action={
                !(status || kind) && (
                  <ButtonLink href="/app/jobs/new" variant="primary" size="sm">
                    New job
                  </ButtonLink>
                )
              }
            />
          }
        >
          {(list) => (
            <>
              <JobsTable jobs={list} />
              <div className="flex flex-col items-center gap-2 px-4 py-4">
                {more.error && <Alert>{more.error}</Alert>}
                {!more.done ? (
                  <Button size="sm" onClick={loadMore} loading={more.busy}>
                    Load more
                  </Button>
                ) : (
                  <p className="text-xs text-subtle">{list.length} jobs shown · end of list</p>
                )}
              </div>
            </>
          )}
        </DataView>
      </Card>
    </>
  );
}
