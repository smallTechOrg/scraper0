"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";
import { Modal } from "./modal";
import { Alert, Badge, Button, Field, Input, Spinner } from "./ui";
import {
  errorMessage,
  sitesApi,
  type Action,
  type ActionStatus,
  type ActionSummary,
  type VersionStatus,
} from "@/lib/api";
import type { ItemRow } from "./items-table";
import type { LiveMode } from "@/lib/live-job";

type Tone = "neutral" | "accent" | "success" | "danger" | "warning";

const STATUS_TONE: Record<ActionStatus | VersionStatus, Tone> = {
  draft: "warning",
  locked: "success",
  broken: "danger",
  retired: "neutral",
};

export function ActionStatusBadge({ status }: { status: ActionStatus | VersionStatus | string }) {
  const tone = STATUS_TONE[status as ActionStatus] ?? "neutral";
  return (
    <Badge tone={tone}>
      {status === "broken" && <span className="size-1.5 rounded-full bg-current" aria-hidden />}
      {status}
    </Badge>
  );
}

export function KindBadge({ kind }: { kind: string }) {
  return <Badge tone={kind === "write" ? "accent" : "neutral"}>{kind}</Badge>;
}

/** One chip per action: name + status, coloured by status. */
export function ActionChips({ actions, empty = "No actions yet" }: { actions: ActionSummary[]; empty?: ReactNode }) {
  if (actions.length === 0) return <span className="text-xs text-subtle">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {actions.map((a) => (
        <Badge key={a.name} tone={STATUS_TONE[a.status] ?? "neutral"} className="font-mono">
          {a.name}
          <span className="font-sans font-normal opacity-80">· {a.status}</span>
        </Badge>
      ))}
    </span>
  );
}

export function LiveIndicator({ mode }: { mode: LiveMode }) {
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

/** Collapsible pretty-printed JSON. */
export function JsonDetails({ label, value }: { label: string; value: unknown }) {
  const empty = value === null || value === undefined || (typeof value === "object" && Object.keys(value).length === 0);
  return (
    <details className="group rounded-md border border-border">
      <summary className="flex cursor-pointer select-none items-center justify-between gap-2 px-3 py-2 text-sm font-medium hover:bg-surface-2">
        {label}
        <span className="text-xs font-normal text-subtle group-open:hidden">Show</span>
        <span className="hidden text-xs font-normal text-subtle group-open:inline">Hide</span>
      </summary>
      {empty ? (
        <p className="border-t border-border px-3 py-2 text-xs text-subtle">Not specified</p>
      ) : (
        <pre className="max-h-80 overflow-auto border-t border-border bg-surface-2 px-3 py-2 font-mono text-xs leading-relaxed">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </details>
  );
}

/**
 * Preview items may be bare entity data or stored items ({id, updated_at, data}).
 * Normalise both into ItemsTable rows.
 */
export function toItemRows(items: Array<Record<string, unknown>>): ItemRow[] {
  return items.map((it, i) => {
    const wrapped = it && typeof it.data === "object" && it.data !== null && !Array.isArray(it.data);
    const d = (wrapped ? it.data : it) as Record<string, unknown>;
    const id = typeof it.id === "string" ? it.id : null;
    return { key: `${id ?? String(d.link ?? d.url ?? "")}:${i}`, data: d };
  });
}

/**
 * Lookup reads (input → output, e.g. track_issue) take an `input` object;
 * list reads (fetch_events) take `options`. The contract doesn't flag this
 * explicitly, so look at the input schema, then fall back to "has an entity".
 */
export function isLookup(action: Pick<Action, "kind" | "entity" | "input_schema">): boolean {
  if (action.kind !== "read") return false;
  const props = action.input_schema?.properties;
  if (props && typeof props === "object") {
    if ("input" in props) return true;
    if ("options" in props) return false;
  }
  return !action.entity;
}

export function normaliseUrl(raw: string): string | null {
  const v = raw.trim();
  if (!v) return null;
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(v) ? v : `https://${v}`;
  try {
    const u = new URL(withScheme);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (!u.hostname.includes(".")) return null;
    return u.toString();
  } catch {
    return null;
  }
}

export const sitePath = (siteId: string, query?: string) =>
  `/app/sites/${encodeURIComponent(siteId)}${query ? `?${query}` : ""}`;

/** "Onboard a site": POST /v2/sites, then go to the site page. */
export function OnboardSiteModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const base = normaliseUrl(url);
    if (!base) {
      setFieldError("Enter a website address, such as https://karmana.in");
      return;
    }
    setFieldError(null);
    setError(null);
    setBusy(true);
    try {
      const { site, existing } = await sitesApi.onboard(base);
      // New sites open the "Add action" dialog; existing ones show the reuse notice.
      router.push(sitePath(site.site_id, existing ? "existing=1" : "add=1"));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => !busy && onClose()}
      title="Onboard a site"
      description="Enter any page on the site. Sites are shared across tenants: if someone already onboarded it, you can reuse its actions."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="onboard-site" loading={busy}>
            Onboard
          </Button>
        </>
      }
    >
      <form id="onboard-site" onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Website URL" error={fieldError} hint="Only the scheme and host are kept, e.g. https://karmana.in">
          {(id, describedBy) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              aria-invalid={!!fieldError || undefined}
              type="url"
              inputMode="url"
              autoComplete="off"
              placeholder="https://karmana.in/events"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              data-autofocus
            />
          )}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

export function JobLink({ id, children }: { id: string; children?: ReactNode }) {
  return (
    <Link href={`/app/jobs/${encodeURIComponent(id)}`} className="text-accent hover:underline">
      {children ?? "Open job"}
    </Link>
  );
}
