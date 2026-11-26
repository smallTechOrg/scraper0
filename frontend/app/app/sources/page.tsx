"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useConfirm } from "@/components/confirm";
import {
  ConnectorForm,
  connectorInputFromState,
  connectorStateFromInput,
  CrawlForm,
  crawlInputFromState,
  crawlStateFromInput,
  defaultConnectorState,
  defaultCrawlState,
  type ConnectorState,
  type CrawlState,
} from "@/components/job-forms";
import { describeJob } from "@/components/jobs";
import { Modal } from "@/components/modal";
import { AdminOnly, useSession } from "@/components/session";
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  cx,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
  StatusBadge,
  Switch,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, errorMessage, type ConnectorInput, type CrawlInput, type Source } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

const MIN_INTERVAL = 15;

function intervalLabel(m: number): string {
  if (m % 1440 === 0) return m === 1440 ? "daily" : `every ${m / 1440} days`;
  if (m % 60 === 0) return m === 60 ? "hourly" : `every ${m / 60} h`;
  return `every ${m} min`;
}

const PRESETS = [
  { label: "15 min", value: 15 },
  { label: "Hourly", value: 60 },
  { label: "6 h", value: 360 },
  { label: "Daily", value: 1440 },
  { label: "Weekly", value: 10080 },
];

function SourceEditor({
  source,
  onClose,
  onSaved,
}: {
  source: Source | null; // null = create
  onClose: () => void;
  onSaved: (s: Source) => void;
}) {
  const [name, setName] = useState(source?.name ?? "");
  const [kind, setKind] = useState<"crawl" | "connector">(source?.kind ?? "crawl");
  const [crawl, setCrawl] = useState<CrawlState>(
    source?.kind === "crawl" ? crawlStateFromInput(source.config as CrawlInput) : defaultCrawlState,
  );
  const [conn, setConn] = useState<ConnectorState>(
    source?.kind === "connector" ? connectorStateFromInput(source.config as ConnectorInput) : defaultConnectorState,
  );
  const [interval, setIntervalMin] = useState(String(source?.interval_minutes ?? 1440));
  const [enabled, setEnabled] = useState(source?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const formId = "source-form";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Give the source a name.");
    const minutes = Number(interval);
    if (!Number.isInteger(minutes) || minutes < MIN_INTERVAL)
      return setError(`Interval must be a whole number of at least ${MIN_INTERVAL} minutes.`);
    const r = kind === "crawl" ? crawlInputFromState(crawl) : connectorInputFromState(conn);
    if ("error" in r) return setError(r.error);
    const body = { name: name.trim(), kind, config: r.input, interval_minutes: minutes, enabled };
    setBusy(true);
    try {
      const saved = source ? await api.updateSource(source.id, body) : await api.createSource(body);
      onSaved(saved);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={source ? `Edit ${source.name}` : "New source"}
      description="A source runs a crawl or connector job on a schedule."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={busy}>
            {source ? "Save changes" : "Create source"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-5">
        <Field label="Name">
          {(id) => (
            <Input id={id} data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="City venues — weekly" />
          )}
        </Field>
        <fieldset>
          <legend className="mb-1.5 text-sm font-medium">Kind</legend>
          <div className="grid grid-cols-2 gap-2 sm:max-w-xs">
            {(["crawl", "connector"] as const).map((k) => (
              <label
                key={k}
                className={cx(
                  "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm",
                  kind === k ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border hover:bg-surface-2",
                )}
              >
                <input
                  type="radio"
                  name="kind"
                  value={k}
                  checked={kind === k}
                  onChange={() => setKind(k)}
                  className="accent-[var(--accent)]"
                />
                {k}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="rounded-lg border border-border p-4">
          {kind === "crawl" ? (
            <CrawlForm value={crawl} onChange={setCrawl} disabled={busy} />
          ) : (
            <ConnectorForm value={conn} onChange={setConn} disabled={busy} />
          )}
        </div>

        <Field label="Run every (minutes)" hint={`At least ${MIN_INTERVAL} minutes.`}>
          {(id, hint) => (
            <div className="space-y-2">
              <Input
                id={id}
                type="number"
                min={MIN_INTERVAL}
                step={1}
                inputMode="numeric"
                aria-describedby={hint}
                value={interval}
                onChange={(e) => setIntervalMin(e.target.value)}
                className="sm:max-w-[10rem]"
              />
              <div className="flex flex-wrap gap-1.5">
                {PRESETS.map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => setIntervalMin(String(p.value))}
                    className={cx(
                      "rounded-full border px-2.5 py-0.5 text-xs",
                      Number(interval) === p.value ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border text-muted hover:bg-surface-2",
                    )}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </Field>
        <Checkbox label="Enabled" hint="Disabled sources keep their settings but don't run on schedule." checked={enabled} onChange={setEnabled} />
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

export default function SourcesPage() {
  const router = useRouter();
  const { canAdmin } = useSession();
  const confirm = useConfirm();
  const sources = useAsync(() => api.sources(), "sources");
  const [editing, setEditing] = useState<Source | null | "new">(null);
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const replace = (s: Source) =>
    sources.setData((prev) => {
      const list = prev ?? [];
      return list.some((x) => x.id === s.id) ? list.map((x) => (x.id === s.id ? s : x)) : [s, ...list];
    });

  async function runNow(s: Source) {
    setActionError(null);
    setRowBusy(s.id);
    try {
      const job = await api.runSource(s.id);
      router.push(`/app/jobs/${encodeURIComponent(job.id)}`);
    } catch (err) {
      setActionError(errorMessage(err));
      setRowBusy(null);
    }
  }

  async function toggle(s: Source, enabled: boolean) {
    setActionError(null);
    setRowBusy(s.id);
    try {
      replace(await api.updateSource(s.id, { enabled }));
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setRowBusy(null);
    }
  }

  async function remove(s: Source) {
    const ok = await confirm({
      title: `Delete “${s.name}”?`,
      message: "The schedule is removed. Past jobs and stored items are kept.",
      confirmLabel: "Delete source",
      action: () => api.deleteSource(s.id),
    });
    if (ok) sources.setData((prev) => (prev ?? []).filter((x) => x.id !== s.id));
  }

  return (
    <>
      <PageHeader
        title="Sources"
        description="Crawls and connector actions that run on a schedule. Results land in Items and the change feed."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setEditing("new")}>
              New source
            </Button>
          </AdminOnly>
        }
      />
      {actionError && <Alert className="mb-4">{actionError}</Alert>}
      <Card>
        <DataView
          state={sources}
          empty={
            <EmptyState
              title="No sources yet"
              description={
                canAdmin
                  ? "Save a crawl or connector as a source to keep its results fresh automatically."
                  : "An admin can set up scheduled sources for this workspace."
              }
              action={
                canAdmin && (
                  <Button variant="primary" size="sm" onClick={() => setEditing("new")}>
                    New source
                  </Button>
                )
              }
            />
          }
        >
          {(list) => (
            <Table>
              <thead>
                <tr>
                  <Th>Source</Th>
                  <Th>Schedule</Th>
                  <Th>Last run</Th>
                  <Th>Enabled</Th>
                  <Th className="text-right">Actions</Th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.id} className="hover:bg-surface-2/60">
                    <Td>
                      <p className="font-medium">{s.name}</p>
                      <p className="mt-0.5 flex items-center gap-1.5 text-xs text-subtle">
                        <Badge>{s.kind}</Badge>
                        <span className="truncate">{describeJob({ kind: s.kind, input: s.config })}</span>
                      </p>
                    </Td>
                    <Td className="whitespace-nowrap">
                      <p>{intervalLabel(s.interval_minutes)}</p>
                      <p className="text-xs text-subtle">
                        {s.enabled && s.next_run_at ? `next ${timeAgo(s.next_run_at)}` : s.enabled ? "pending" : "paused"}
                      </p>
                    </Td>
                    <Td className="whitespace-nowrap">
                      {s.last_job ? (
                        <Link href={`/app/jobs/${encodeURIComponent(s.last_job.id)}`} className="inline-flex items-center gap-2 hover:underline">
                          <StatusBadge status={s.last_job.status} />
                          <span className="text-xs text-muted">{timeAgo(s.last_job.created_at)}</span>
                        </Link>
                      ) : (
                        <span className="text-xs text-subtle">Never</span>
                      )}
                    </Td>
                    <Td>
                      <Switch
                        checked={s.enabled}
                        label={`${s.enabled ? "Disable" : "Enable"} ${s.name}`}
                        disabled={!canAdmin || rowBusy === s.id}
                        onChange={(v) => toggle(s, v)}
                      />
                    </Td>
                    <Td className="text-right">
                      <div className="flex justify-end gap-1.5">
                        <Button size="sm" onClick={() => runNow(s)} loading={rowBusy === s.id}>
                          Run now
                        </Button>
                        <AdminOnly>
                          <Button size="sm" variant="ghost" onClick={() => setEditing(s)}>
                            Edit
                          </Button>
                          <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => remove(s)}>
                            Delete
                          </Button>
                        </AdminOnly>
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </DataView>
      </Card>

      {editing && (
        <SourceEditor
          source={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(s) => {
            replace(s);
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
