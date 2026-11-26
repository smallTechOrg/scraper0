"use client";

import { useEffect, useMemo } from "react";
import {
  actionName,
  api,
  type Connector,
  type ConnectorInput,
  type CrawlInput,
  type CrawlOptions,
  type EntitySpec,
} from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import { Alert, Button, Checkbox, ErrorState, Field, Input, LoadingState, Select, Textarea } from "./ui";

export const MAX_URLS = 20;

// ---------------------------------------------------------------------------
// JSON editor
// ---------------------------------------------------------------------------

export function parseJson(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  if (!text.trim()) return { ok: true, value: undefined };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "Invalid JSON" };
  }
}

export function JsonEditor({
  id,
  value,
  onChange,
  rows = 8,
  describedBy,
  placeholder,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  describedBy?: string;
  placeholder?: string;
}) {
  const parsed = parseJson(value);
  return (
    <div className="space-y-1.5">
      <Textarea
        id={id}
        rows={rows}
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        aria-describedby={describedBy}
        aria-invalid={!parsed.ok || undefined}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono text-xs leading-relaxed"
      />
      <div className="flex items-center justify-between gap-2">
        <p className={parsed.ok ? "text-xs text-subtle" : "text-xs text-danger"} aria-live="polite">
          {parsed.ok ? (value.trim() ? "Valid JSON" : "Empty") : `Invalid JSON: ${parsed.error}`}
        </p>
        <Button
          size="sm"
          variant="ghost"
          disabled={!parsed.ok || !value.trim()}
          onClick={() => parsed.ok && onChange(JSON.stringify(parsed.value, null, 2))}
        >
          Format
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Crawl
// ---------------------------------------------------------------------------

export interface CrawlState {
  entity: string;
  urls: string;
  max_detail_pages: string;
  max_list_pages: string;
  allow_llm: boolean;
  learn: boolean;
  use_cache: boolean;
  include_past: boolean;
  include_online: boolean;
  event_filter: string;
  link_pattern: string;
}

export const defaultCrawlState: CrawlState = {
  entity: "event",
  urls: "",
  max_detail_pages: "25",
  max_list_pages: "",
  allow_llm: true,
  learn: true,
  use_cache: true,
  include_past: false,
  include_online: true,
  event_filter: "",
  link_pattern: "",
};

export function crawlStateFromInput(input: Partial<CrawlInput> | null | undefined): CrawlState {
  const o: CrawlOptions = input?.options ?? {};
  const entity = typeof input?.entity === "string" ? input.entity : input?.entity?.name;
  return {
    entity: entity ?? defaultCrawlState.entity,
    urls: (input?.urls ?? []).join("\n"),
    max_detail_pages: o.max_detail_pages !== undefined ? String(o.max_detail_pages) : "",
    max_list_pages: o.max_list_pages !== undefined ? String(o.max_list_pages) : "",
    allow_llm: o.allow_llm ?? defaultCrawlState.allow_llm,
    learn: o.learn ?? defaultCrawlState.learn,
    use_cache: o.use_cache ?? defaultCrawlState.use_cache,
    include_past: o.include_past ?? defaultCrawlState.include_past,
    include_online: o.include_online ?? defaultCrawlState.include_online,
    event_filter: o.event_filter ?? "",
    link_pattern: o.link_pattern ?? "",
  };
}

export function splitUrls(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function isHttpUrl(s: string): boolean {
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export function crawlInputFromState(s: CrawlState): { input: CrawlInput } | { error: string } {
  if (!s.entity) return { error: "Choose an entity." };
  const urls = splitUrls(s.urls);
  if (urls.length === 0) return { error: "Add at least one URL." };
  if (urls.length > MAX_URLS) return { error: `At most ${MAX_URLS} URLs per job.` };
  const bad = urls.find((u) => !isHttpUrl(u));
  if (bad) return { error: `Not a valid http(s) URL: ${bad}` };
  const options: CrawlOptions = {
    allow_llm: s.allow_llm,
    learn: s.learn,
    use_cache: s.use_cache,
  };
  const num = (v: string) => (v.trim() ? Number(v) : undefined);
  const mdp = num(s.max_detail_pages);
  const mlp = num(s.max_list_pages);
  if (mdp !== undefined) {
    if (!Number.isInteger(mdp) || mdp < 0) return { error: "Max detail pages must be a whole number." };
    options.max_detail_pages = mdp;
  }
  if (mlp !== undefined) {
    if (!Number.isInteger(mlp) || mlp < 0) return { error: "Max list pages must be a whole number." };
    options.max_list_pages = mlp;
  }
  if (s.entity === "event") {
    options.include_past = s.include_past;
    options.include_online = s.include_online;
    if (s.event_filter.trim()) options.event_filter = s.event_filter.trim();
  } else if (s.link_pattern.trim()) {
    try {
      new RegExp(s.link_pattern.trim());
    } catch {
      return { error: "Link pattern is not a valid regular expression." };
    }
    options.link_pattern = s.link_pattern.trim();
  }
  return { input: { entity: s.entity, urls, options } };
}

export function CrawlForm({
  value,
  onChange,
  disabled,
}: {
  value: CrawlState;
  onChange: (v: CrawlState) => void;
  disabled?: boolean;
}) {
  const entities = useAsync(() => api.entities(), "entities");
  const set = <K extends keyof CrawlState>(k: K, v: CrawlState[K]) => onChange({ ...value, [k]: v });
  const urlCount = splitUrls(value.urls).length;

  // If the selected entity doesn't exist (e.g. default "event" removed), pick the first.
  const names = useMemo(() => (entities.data ?? []).map((e) => e.name), [entities.data]);
  useEffect(() => {
    if (names.length && !names.includes(value.entity)) onChange({ ...value, entity: names[0] });
  }, [names, value, onChange]);

  return (
    <fieldset className="space-y-5" disabled={disabled}>
      <Field label="Entity" hint="What to extract from each page.">
        {(id, hint) =>
          entities.error ? (
            <ErrorState error={entities.error} onRetry={entities.reload} />
          ) : !entities.data ? (
            <LoadingState label="Loading entities…" />
          ) : (
            <Select id={id} aria-describedby={hint} value={value.entity} onChange={(e) => set("entity", e.target.value)}>
              <EntityOptions entities={entities.data} />
            </Select>
          )
        }
      </Field>

      <Field
        label="URLs"
        hint={`One per line — list pages or detail pages. ${urlCount}/${MAX_URLS}.`}
        error={urlCount > MAX_URLS ? `At most ${MAX_URLS} URLs per job.` : null}
      >
        {(id, hint) => (
          <Textarea
            id={id}
            rows={5}
            aria-describedby={hint}
            placeholder={"https://example.org/events\nhttps://example.org/whats-on/page/2"}
            value={value.urls}
            onChange={(e) => set("urls", e.target.value)}
            className="font-mono text-xs"
          />
        )}
      </Field>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Max detail pages" hint="Upper bound on pages opened per job.">
          {(id, hint) => (
            <Input
              id={id}
              type="number"
              min={0}
              inputMode="numeric"
              aria-describedby={hint}
              value={value.max_detail_pages}
              onChange={(e) => set("max_detail_pages", e.target.value)}
            />
          )}
        </Field>
        <Field label="Max list pages" optional hint="How many pagination pages to follow.">
          {(id, hint) => (
            <Input
              id={id}
              type="number"
              min={0}
              inputMode="numeric"
              aria-describedby={hint}
              value={value.max_list_pages}
              onChange={(e) => set("max_list_pages", e.target.value)}
            />
          )}
        </Field>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Checkbox
          label="Allow AI"
          hint="Use the LLM when no layout is known."
          checked={value.allow_llm}
          onChange={(v) => set("allow_llm", v)}
        />
        <Checkbox
          label="Learn layouts"
          hint="Save new layouts as recipes."
          checked={value.learn}
          onChange={(v) => set("learn", v)}
        />
        <Checkbox
          label="Use cache"
          hint="Reuse recently fetched pages."
          checked={value.use_cache}
          onChange={(v) => set("use_cache", v)}
        />
      </div>

      {value.entity !== "event" && (
        <Field
          label="Link pattern"
          optional
          hint="Regex on URL paths of detail pages, e.g. ^/jobs/\d+ — helps find the right links on list pages."
        >
          {(id, hint) => (
            <Input
              id={id}
              aria-describedby={hint}
              className="font-mono text-xs"
              value={value.link_pattern}
              onChange={(e) => set("link_pattern", e.target.value)}
            />
          )}
        </Field>
      )}

      {value.entity === "event" && (
        <div className="space-y-4 rounded-lg border border-border bg-surface-2/50 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-subtle">Event options</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Checkbox label="Include past events" checked={value.include_past} onChange={(v) => set("include_past", v)} />
            <Checkbox
              label="Include online events"
              checked={value.include_online}
              onChange={(v) => set("include_online", v)}
            />
          </div>
          <Field label="Event filter" optional hint="Free text, e.g. “volunteering or clean-up drives in Bengaluru”.">
            {(id, hint) => (
              <Input
                id={id}
                aria-describedby={hint}
                value={value.event_filter}
                onChange={(e) => set("event_filter", e.target.value)}
              />
            )}
          </Field>
        </div>
      )}
    </fieldset>
  );
}

export function EntityOptions({ entities }: { entities: EntitySpec[] }) {
  const builtin = entities.filter((e) => e.builtin);
  const custom = entities.filter((e) => !e.builtin);
  return (
    <>
      {builtin.length > 0 && (
        <optgroup label="Built-in">
          {builtin.map((e) => (
            <option key={e.name} value={e.name}>
              {e.name}
            </option>
          ))}
        </optgroup>
      )}
      {custom.length > 0 && (
        <optgroup label="Custom">
          {custom.map((e) => (
            <option key={e.name} value={e.name}>
              {e.name}
            </option>
          ))}
        </optgroup>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Connector
// ---------------------------------------------------------------------------

export interface ConnectorState {
  connector: string;
  action: string;
  data: string;
  auth: string;
}

export const defaultConnectorState: ConnectorState = { connector: "", action: "", data: "{}", auth: "" };

export function connectorStateFromInput(input: Partial<ConnectorInput> | null | undefined): ConnectorState {
  return {
    connector: input?.connector ?? "",
    action: input?.action ?? "",
    data: input?.data !== undefined ? JSON.stringify(input.data, null, 2) : "{}",
    // The API masks stored credentials as "***"; never round-trip the mask.
    auth: input?.auth !== undefined && input.auth !== null && input.auth !== "***" ? JSON.stringify(input.auth, null, 2) : "",
  };
}

export function connectorInputFromState(s: ConnectorState): { input: ConnectorInput } | { error: string } {
  if (!s.connector) return { error: "Choose a connector." };
  if (!s.action) return { error: "Choose an action." };
  const data = parseJson(s.data);
  if (!data.ok) return { error: `Data is not valid JSON: ${data.error}` };
  const auth = parseJson(s.auth);
  if (!auth.ok) return { error: `Auth is not valid JSON: ${auth.error}` };
  const input: ConnectorInput = { connector: s.connector, action: s.action, data: data.value ?? {} };
  if (auth.value !== undefined) input.auth = auth.value;
  return { input };
}

export function ConnectorForm({
  value,
  onChange,
  disabled,
}: {
  value: ConnectorState;
  onChange: (v: ConnectorState) => void;
  disabled?: boolean;
}) {
  const connectors = useAsync(() => api.connectors(), "connectors");
  const list: Connector[] = useMemo(() => connectors.data ?? [], [connectors.data]);
  const selected = list.find((c) => c.name === value.connector);
  const actions = (selected?.actions ?? []).map((a) => ({
    name: actionName(a),
    description: typeof a === "string" ? undefined : a.description,
  }));

  // Default to the first connector/action once loaded.
  useEffect(() => {
    if (!list.length) return;
    const c = list.find((x) => x.name === value.connector) ?? list[0];
    const names = c.actions.map(actionName);
    const action = names.includes(value.action) ? value.action : (names[0] ?? "");
    if (c.name !== value.connector || action !== value.action) onChange({ ...value, connector: c.name, action });
  }, [list, value, onChange]);

  if (connectors.error) return <ErrorState error={connectors.error} onRetry={connectors.reload} />;
  if (!connectors.data) return <LoadingState label="Loading connectors…" />;
  if (list.length === 0) return <Alert tone="warning">No connectors are available on this server.</Alert>;

  return (
    <fieldset className="space-y-5" disabled={disabled}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Connector">
          {(id) => (
            <Select
              id={id}
              value={value.connector}
              onChange={(e) => {
                const c = list.find((x) => x.name === e.target.value);
                onChange({ ...value, connector: e.target.value, action: c ? (c.actions.map(actionName)[0] ?? "") : "" });
              }}
            >
              {list.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Action">
          {(id) => (
            <Select id={id} value={value.action} onChange={(e) => onChange({ ...value, action: e.target.value })}>
              {actions.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </div>
      {selected && (
        <p className="text-sm text-muted">
          {selected.description}
          {selected.source && <span className="ml-1 text-subtle">({selected.source})</span>}
          {selected.needs_browser && <span className="ml-1 text-subtle">· runs a headless browser, slower</span>}
          {actions.find((a) => a.name === value.action)?.description && (
            <span className="mt-1 block">{actions.find((a) => a.name === value.action)?.description}</span>
          )}
        </p>
      )}
      <Field label="Data" hint="JSON payload passed to the connector action.">
        {(id, hint) => (
          <JsonEditor id={id} describedBy={hint} value={value.data} onChange={(v) => onChange({ ...value, data: v })} />
        )}
      </Field>
      <Field label="Auth" optional hint="JSON credentials, only for connectors that need a login.">
        {(id, hint) => (
          <JsonEditor
            id={id}
            rows={3}
            describedBy={hint}
            placeholder='{"username": "…", "password": "…"}'
            value={value.auth}
            onChange={(v) => onChange({ ...value, auth: v })}
          />
        )}
      </Field>
    </fieldset>
  );
}
