"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useId, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  ConnectorForm,
  connectorInputFromState,
  CrawlForm,
  crawlInputFromState,
  defaultConnectorState,
  defaultCrawlState,
  EntityOptions,
  splitUrls,
  type ConnectorState,
  type CrawlState,
} from "@/components/job-forms";
import { Alert, Button, Card, cx, Field, Input, PageHeader, Select, Textarea } from "@/components/ui";
import { api, errorMessage, type Job } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

type Tab = "crawl" | "connector" | "upload";
const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: "crawl", label: "Crawl", hint: "Extract entities from web pages" },
  { id: "upload", label: "Upload", hint: "Posters, PDFs, spreadsheets, text" },
  { id: "connector", label: "Connector", hint: "Run a site-specific action" },
];

const MAX_FILES = 10;
const ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.heic,.pdf,.csv,.json,.ics,.txt,image/*,application/pdf,text/csv,application/json,text/calendar,text/plain";
const ACCEPT_EXT = /\.(png|jpe?g|webp|gif|heic|pdf|csv|json|ics|txt)$/i;

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function UploadForm({ onSubmit, busy }: { onSubmit: (form: FormData) => void; busy: boolean }) {
  const entities = useAsync(() => api.entities(), "entities");
  const [files, setFiles] = useState<File[]>([]);
  const [text, setText] = useState("");
  const [urls, setUrls] = useState("");
  const [entity, setEntity] = useState("event");
  const [portal, setPortal] = useState("");
  const [drag, setDrag] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dropId = useId();

  function addFiles(list: FileList | File[]) {
    setError(null);
    const incoming = Array.from(list);
    const rejected = incoming.filter((f) => !ACCEPT_EXT.test(f.name) && !f.type.startsWith("image/"));
    const ok = incoming.filter((f) => !rejected.includes(f));
    setFiles((prev) => {
      const merged = [...prev];
      for (const f of ok) if (!merged.some((m) => m.name === f.name && m.size === f.size)) merged.push(f);
      if (merged.length > MAX_FILES) setError(`At most ${MAX_FILES} files per job; extra files were skipped.`);
      return merged.slice(0, MAX_FILES);
    });
    if (rejected.length) setError(`Unsupported file type: ${rejected.map((f) => f.name).join(", ")}`);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDrag(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const u = splitUrls(urls);
    if (files.length === 0 && !text.trim() && u.length === 0) {
      setError("Add at least one file, some text or a URL.");
      return;
    }
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    if (text.trim()) form.append("text", text);
    if (u.length) form.append("urls", u.join("\n"));
    form.append("entity", entity);
    if (portal.trim()) form.append("portal", portal.trim());
    onSubmit(form);
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div>
        <p id={dropId} className="mb-1.5 text-sm font-medium">
          Files
        </p>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          className={cx(
            "flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors",
            drag ? "border-accent bg-accent-soft" : "border-border-strong bg-surface-2/40",
          )}
        >
          <svg viewBox="0 0 24 24" className="size-8 text-subtle" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
            <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <p className="mt-2 text-sm">
            Drag and drop files here, or{" "}
            <button
              type="button"
              className="font-medium text-accent hover:underline"
              onClick={() => inputRef.current?.click()}
              aria-describedby={dropId}
            >
              browse
            </button>
          </p>
          <p className="mt-1 text-xs text-subtle">
            Posters &amp; images, PDF, CSV, JSON, ICS or TXT · up to {MAX_FILES} files
          </p>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPT}
            className="sr-only"
            tabIndex={-1}
            aria-hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
        {files.length > 0 && (
          <ul className="mt-3 divide-y divide-border rounded-md border border-border">
            {files.map((f) => (
              <li key={f.name + f.size} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <span className="min-w-0 truncate">{f.name}</span>
                <span className="flex shrink-0 items-center gap-3 text-xs text-subtle">
                  {formatBytes(f.size)}
                  <button
                    type="button"
                    className="rounded px-1.5 py-0.5 text-muted hover:bg-surface-2 hover:text-danger"
                    onClick={() => setFiles((fs) => fs.filter((x) => x !== f))}
                    aria-label={`Remove ${f.name}`}
                  >
                    Remove
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <Field label="Pasted text" optional hint="A newsletter, an email, a list of events — anything with the data in it.">
        {(id, hint) => <Textarea id={id} rows={5} aria-describedby={hint} value={text} onChange={(e) => setText(e.target.value)} />}
      </Field>

      <Field label="URLs of files" optional hint="Links to images, PDFs or feeds (e.g. an .ics calendar), one per line.">
        {(id, hint) => (
          <Textarea
            id={id}
            rows={3}
            aria-describedby={hint}
            className="font-mono text-xs"
            value={urls}
            onChange={(e) => setUrls(e.target.value)}
          />
        )}
      </Field>

      <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Entity">
        {(id) => (
          <Select id={id} value={entity} onChange={(e) => setEntity(e.target.value)} disabled={!entities.data}>
            {entities.data ? <EntityOptions entities={entities.data} /> : <option value="event">event</option>}
          </Select>
        )}
      </Field>
      <Field label="Portal" optional hint="Label stored with the results, e.g. the source's name.">
        {(id, hint) => <Input id={id} aria-describedby={hint} value={portal} onChange={(e) => setPortal(e.target.value)} />}
      </Field>
      </div>

      {error && <Alert>{error}</Alert>}
      <Button type="submit" variant="primary" loading={busy}>
        Start upload job
      </Button>
    </form>
  );
}

export function NewJob() {
  const router = useRouter();
  const params = useSearchParams();
  const initial = (params.get("tab") as Tab) || "crawl";
  const [tab, setTab] = useState<Tab>(TABS.some((t) => t.id === initial) ? initial : "crawl");
  const [crawl, setCrawl] = useState<CrawlState>(defaultCrawlState);
  const [conn, setConn] = useState<ConnectorState>(defaultConnectorState);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(create: () => Promise<Job>) {
    setBusy(true);
    setError(null);
    try {
      const job = await create();
      router.push(`/app/jobs/${encodeURIComponent(job.id)}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  function submitCrawl(e: FormEvent) {
    e.preventDefault();
    const r = crawlInputFromState(crawl);
    if ("error" in r) return setError(r.error);
    run(() => api.createJob({ kind: "crawl", input: r.input }));
  }

  function submitConnector(e: FormEvent) {
    e.preventDefault();
    const r = connectorInputFromState(conn);
    if ("error" in r) return setError(r.error);
    run(() => api.createJob({ kind: "connector", input: r.input }));
  }

  return (
    <>
      <PageHeader title="New job" back={{ href: "/app/jobs", label: "Jobs" }} />
      <div role="tablist" aria-label="Job type" className="mb-4 grid grid-cols-3 gap-2 sm:max-w-xl">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            onClick={() => {
              setTab(t.id);
              setError(null);
            }}
            className={cx(
              "rounded-lg border px-3 py-2 text-left transition-colors",
              tab === t.id ? "border-accent bg-accent-soft" : "border-border bg-surface hover:bg-surface-2",
            )}
          >
            <span className={cx("block text-sm font-medium", tab === t.id && "text-accent-soft-fg")}>{t.label}</span>
            <span className="hidden text-xs text-subtle sm:block">{t.hint}</span>
          </button>
        ))}
      </div>

      <Card className="p-4 sm:p-6">
        <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === "crawl" && (
            <form onSubmit={submitCrawl} className="space-y-5">
              <CrawlForm value={crawl} onChange={setCrawl} disabled={busy} />
              {error && <Alert>{error}</Alert>}
              <Button type="submit" variant="primary" loading={busy}>
                Start crawl
              </Button>
            </form>
          )}
          {tab === "connector" && (
            <form onSubmit={submitConnector} className="space-y-5">
              <ConnectorForm value={conn} onChange={setConn} disabled={busy} />
              {error && <Alert>{error}</Alert>}
              <Button type="submit" variant="primary" loading={busy}>
                Run connector
              </Button>
            </form>
          )}
          {tab === "upload" && (
            <>
              <UploadForm busy={busy} onSubmit={(form) => run(() => api.ingest(form))} />
              {error && <Alert className="mt-4">{error}</Alert>}
            </>
          )}
        </div>
      </Card>
    </>
  );
}
