"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useConfirm } from "@/components/confirm";
import { Modal } from "@/components/modal";
import { AdminOnly, useSession } from "@/components/session";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  cx,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
  Select,
  StatusBadge,
  Switch,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, errorMessage, sitesApi, type SiteSummary, type Source } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

const MIN_INTERVAL = 15;

/** Locked (or broken, which keeps serving its locked version) list actions: what a schedule can refresh. */
function readActions(site: SiteSummary): string[] {
  return site.actions
    .filter((a) => a.kind === "read" && (a.mode ?? "list") === "list" && (a.status === "locked" || a.status === "broken"))
    .map((a) => a.name);
}

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
  const [siteId, setSiteId] = useState(source?.site_id ?? "");
  const [action, setAction] = useState(source?.action ?? "fetch_events");
  const [interval, setIntervalMin] = useState(String(source?.interval_minutes ?? 1440));
  const [enabled, setEnabled] = useState(source?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const formId = "source-form";
  const sites = useAsync(() => sitesApi.list(), "sites-for-sources");
  const usable: SiteSummary[] = (sites.data ?? []).filter((x) => readActions(x).length > 0);
  const chosen = usable.find((x) => x.site_id === siteId);
  const actions = chosen ? readActions(chosen) : [];

  function pickSite(id: string) {
    setSiteId(id);
    const site = usable.find((x) => x.site_id === id);
    const names = site ? readActions(site) : [];
    if (names.length && !names.includes(action)) setAction(names[0]);
    if (!name.trim() && site) setName(site.name);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Give the source a name.");
    if (!siteId) return setError("Choose an onboarded site.");
    const minutes = Number(interval);
    if (!Number.isInteger(minutes) || minutes < MIN_INTERVAL)
      return setError(`Interval must be a whole number of at least ${MIN_INTERVAL} minutes.`);
    const body = { name: name.trim(), site_id: siteId, action, interval_minutes: minutes, enabled };
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
      title={source ? `Edit ${source.name}` : "New schedule"}
      description="Refreshes a locked site action on a schedule. Only onboarded sites with a locked read action can be scheduled."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={busy}>
            {source ? "Save changes" : "Create schedule"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-5">
        <Field label="Site" hint={chosen ? chosen.base_url : "Sites with a locked read action."}>
          {(id, hint) => (
            <Select id={id} aria-describedby={hint} value={siteId} onChange={(e) => pickSite(e.target.value)} disabled={busy}>
              <option value="">{sites.loading ? "Loading sites…" : "Choose a site"}</option>
              {usable.map((x) => (
                <option key={x.site_id} value={x.site_id}>
                  {x.name} ({x.site_id})
                </option>
              ))}
            </Select>
          )}
        </Field>
        {sites.data && usable.length === 0 && (
          <Alert tone="warning">
            No site has a locked read action yet.{" "}
            <Link href="/app/sites" className="underline">
              Onboard a site
            </Link>{" "}
            and lock an action first.
          </Alert>
        )}
        <Field label="Action">
          {(id) => (
            <Select id={id} value={action} onChange={(e) => setAction(e.target.value)} disabled={busy || !chosen}>
              {(actions.length ? actions : [action]).map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Name">
          {(id) => (
            <Input id={id} data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Karmana — every 6 h" />
          )}
        </Field>

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
        <Checkbox label="Enabled" hint="Disabled schedules keep their settings but don't run." checked={enabled} onChange={setEnabled} />
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
      if (job.id) router.push(`/app/jobs/${encodeURIComponent(job.id)}`);
      else {
        setActionError("Joined a refresh another workspace started; its results land in this site's items.");
        setRowBusy(null);
        sources.reload();
      }
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
      confirmLabel: "Delete schedule",
      action: () => api.deleteSource(s.id),
    });
    if (ok) sources.setData((prev) => (prev ?? []).filter((x) => x.id !== s.id));
  }

  return (
    <>
      <PageHeader
        title="Sources"
        description="Schedules on onboarded site actions. Each run refreshes the locked version; results land in Items and the change feed."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setEditing("new")}>
              New schedule
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
                  ? "Schedule a locked site action to keep its items fresh automatically."
                  : "An admin can schedule site actions for this workspace."
              }
              action={
                canAdmin && (
                  <Button variant="primary" size="sm" onClick={() => setEditing("new")}>
                    New schedule
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
                      <p className="mt-0.5 text-xs text-subtle">
                        <Link
                          href={`/app/sites/${encodeURIComponent(s.site_id)}?action=${encodeURIComponent(s.action)}`}
                          className="hover:text-accent hover:underline"
                        >
                          {s.site_id} · {s.action}
                        </Link>
                      </p>
                    </Td>
                    <Td className="whitespace-nowrap">
                      <p>{intervalLabel(s.interval_minutes)}</p>
                      <p className="text-xs text-subtle">
                        {s.enabled && s.next_run_at ? `next ${timeAgo(s.next_run_at)}` : s.enabled ? "pending" : "paused"}
                      </p>
                    </Td>
                    <Td className="whitespace-nowrap">
                      {s.last_job?.id ? (
                        <Link href={`/app/jobs/${encodeURIComponent(s.last_job.id)}`} className="inline-flex items-center gap-2 hover:underline">
                          <StatusBadge status={s.last_job.status} />
                          <span className="text-xs text-muted">{timeAgo(s.last_job.created_at)}</span>
                        </Link>
                      ) : s.last_job ? (
                        <span className="inline-flex items-center gap-2" title="Joined a refresh another workspace started">
                          <StatusBadge status={s.last_job.status} />
                          <span className="text-xs text-muted">shared refresh</span>
                        </span>
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
