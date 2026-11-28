"use client";

/**
 * How a version runs, made readable: the recipe (plans) of a learned version, or the code connector of a code
 * version. Plans are documented in spec/recipes.md; this view reads them loosely (unknown keys are shown raw).
 */
import { Fragment, useState, type ReactNode } from "react";
import { Badge, Button, CopyButton, cx } from "@/components/ui";

type Json = unknown;
type Dict = Record<string, Json>;

export interface PlanStep {
  id?: string;
  type: string;
  method?: string;
  url?: string;
  query?: Dict;
  headers?: Dict;
  body?: { json?: Json; form?: Dict; text?: string };
  request?: PlanStep;
  paginate?: Dict;
  items?: Dict;
  extract?: { structured?: boolean; selectors?: Array<{ path_pattern?: string; rules?: Dict }> };
  capture?: Array<Dict>;
  retry?: { attempts?: number; backoff_s?: number };
  expect?: Dict;
  on_failure?: string | { goto: string };
  when?: Dict;
  alternatives?: PlanStep[];
  over?: string;
  max?: number;
  optional?: boolean;
  missing?: string[];
  fallback_only?: boolean;
  uses_ai?: boolean;
  fields?: string[];
  field?: string;
  key?: string;
  id_key?: string;
  value?: string;
}

export interface Plan {
  plan_version?: number;
  entity?: string;
  entry?: { direct?: string; root?: string; navigate?: Array<{ link?: { text?: string[]; href_contains?: string[] } }> };
  steps?: PlanStep[];
  output?: string;
  mapping?: Dict;
  link_template?: string;
  link_base?: string;
  learned_by?: string;
  placeholders?: Record<string, string[]>;
}

export interface Snapshot {
  implementation?: string;
  connector?: string;
  action?: string;
  plans?: Array<{ seed: string; plan: Plan }>;
  learn?: { ai_calls?: number; notes?: string[]; by_method?: Record<string, number> };
  check_run?: RunStats[];
}

export interface RunStats {
  seed?: string;
  requests?: number;
  pages?: number;
  items?: number;
  detail_failures?: number;
  ai_calls?: number;
  ai_tokens?: number;
  ai_by_step?: Record<string, { calls: number; tokens: number }>;
  entry?: string;
  entry_changed_to?: string | null;
  alternatives?: Record<string, number>;
  retries?: Record<string, number>;
  expect_failures?: Array<{ step: string; reason: string }>;
  outcomes?: Record<string, string>;
  truncated?: Array<{ step: string; cap: string; items: number; total?: number | null }>;
  paging?: Record<string, { unique: number; total?: number | null; sweeps: number }>;
}

export function hasRecipe(snapshot: Record<string, unknown> | null | undefined): boolean {
  const s = snapshot as Snapshot | null | undefined;
  return !!s && (s.implementation === "code" || (s.plans?.length ?? 0) > 0);
}

// ------------------------------------------------------------------ templated values

/** A string with {placeholders} highlighted: these are filled at run time (page, today, captures, item…). */
function Templated({ text }: { text: string }) {
  const parts = text.split(/(\{[A-Za-z_][\w.\-]*\})/g);
  return (
    <>
      {parts.map((p, i) =>
        /^\{[A-Za-z_][\w.\-]*\}$/.test(p) ? (
          <span key={i} className="rounded bg-accent-soft px-1 text-accent-soft-fg" title="filled in at run time">
            {p}
          </span>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  );
}

/** JSON with templated strings highlighted. */
function JsonValue({ value, indent = 0 }: { value: Json; indent?: number }): ReactNode {
  const pad = "  ".repeat(indent);
  if (typeof value === "string") return <>&quot;<Templated text={value} />&quot;</>;
  if (value === null || typeof value !== "object") return <>{JSON.stringify(value)}</>;
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as Dict);
  if (entries.length === 0) return <>{Array.isArray(value) ? "[]" : "{}"}</>;
  const [open, close] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];
  return (
    <>
      {open}
      {"\n"}
      {entries.map(([k, v], i) => (
        <Fragment key={k}>
          {pad}
          {"  "}
          {!Array.isArray(value) && <span className="text-subtle">&quot;{k}&quot;: </span>}
          <JsonValue value={v} indent={indent + 1} />
          {i < entries.length - 1 ? "," : ""}
          {"\n"}
        </Fragment>
      ))}
      {pad}
      {close}
    </>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <code className="break-all font-mono text-xs">{children}</code>;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2 text-xs">
      <div className="text-subtle">{label}</div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

// ------------------------------------------------------------------ one step

function paginateText(pg: Dict): string {
  const mode = String(pg.mode ?? "");
  const parts = [mode];
  if (mode === "offset") parts.push(`from ${pg.start ?? 0}${pg.step ? `, +${pg.step}` : ""}`);
  if (mode === "page") parts.push(`from ${pg.start ?? 1}`);
  if (mode === "cursor") parts.push(`next cursor at ${pg.cursor_path}`);
  const stop = (pg.stop as Dict | undefined) ?? {};
  const stops: string[] = [];
  if (stop.fewer_than) stops.push(`a page has < ${stop.fewer_than} items`);
  if (stop.total_path) stops.push(`${stop.total_path} reached`);
  stops.push(pg.max_pages ? `${pg.max_pages} pages at most` : "a page is empty");
  return `${parts.join(" ")} · stops when ${stops.join(", or ")}`;
}

function stepTitle(st: PlanStep): string {
  return { request: "Request", for_each: "For each", lookup: "Lookup", any_of: "Any of", ai: "AI fill", captcha: "Captcha" }[
    st.type
  ] ?? st.type;
}

function StepCard({ step, index, alt }: { step: PlanStep; index?: number; alt?: boolean }) {
  const req: PlanStep | undefined = step.type === "request" ? step : step.request;
  const aiStep = step.type === "ai" || step.type === "captcha" || step.uses_ai;
  return (
    <div className={cx("rounded-md border border-border bg-surface p-3", alt && "bg-surface-2/50")}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {index !== undefined && <span className="font-mono text-xs text-subtle">{index + 1}.</span>}
        <span className="text-sm font-medium">{stepTitle(step)}</span>
        {step.id && <Mono>{step.id}</Mono>}
        {aiStep ? <Badge tone="warning">uses AI</Badge> : <Badge tone="success">no AI</Badge>}
        {step.optional && <Badge>optional</Badge>}
        {step.fallback_only && <Badge>fallback only</Badge>}
      </div>
      <div className="space-y-1.5">
        {req?.url && (
          <Row label={(req.method ?? "GET").toUpperCase()}>
            <Mono>
              <Templated text={req.url} />
            </Mono>
          </Row>
        )}
        {req?.query && Object.keys(req.query).length > 0 && (
          <Row label="Query">
            <pre className="whitespace-pre-wrap font-mono text-xs">
              <JsonValue value={req.query} />
            </pre>
          </Row>
        )}
        {req?.headers && Object.keys(req.headers).length > 0 && (
          <Row label="Headers">
            <Mono>
              {Object.entries(req.headers).map(([k, v]) => (
                <div key={k}>
                  {k}: <Templated text={String(v)} />
                </div>
              ))}
            </Mono>
          </Row>
        )}
        {req?.body && (
          <Row label={`Body (${Object.keys(req.body)[0]})`}>
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-surface-2 p-2 font-mono text-xs">
              <JsonValue value={Object.values(req.body)[0]} />
            </pre>
          </Row>
        )}
        {step.paginate && <Row label="Paging">{paginateText(step.paginate)}{step.paginate.unstable_order ? " · order not stable between calls: swept again until every item was seen" : ""}</Row>}
        {step.items && (
          <Row label="Items">
            {"json" in step.items ? (
              <Mono>JSON {String(step.items.json) || "(root)"}</Mono>
            ) : (
              <span>
                links matching{" "}
                <Mono>{(((step.items.links as Dict | undefined)?.patterns as string[] | undefined) ?? []).join(", ") || "any"}</Mono>
              </span>
            )}
          </Row>
        )}
        {step.type === "for_each" && (
          <Row label="Over">
            <Mono>{step.over}</Mono> · at most {step.max ?? 120}
            {(step.missing ?? []).length > 0 && ` · only items without ${(step.missing ?? []).join(", ")}`}
          </Row>
        )}
        {step.extract && (
          <Row label="Reads">
            {[step.extract.structured && "structured data", ...(step.extract.selectors ?? []).map(
              (s) => `CSS rules for ${s.path_pattern ?? "any page"} (${Object.keys(s.rules ?? {}).join(", ")})`,
            )]
              .filter(Boolean)
              .join(" · ")}
          </Row>
        )}
        {step.type === "lookup" && (
          <Row label="Join">
            <Mono>
              item.{step.key} → {step.id_key} · {step.value} into {step.field}
            </Mono>
          </Row>
        )}
        {step.type === "ai" && (
          <Row label="Fills">
            {(step.fields ?? []).join(", ")} for items of <Mono>{step.over}</Mono> that lack them
          </Row>
        )}
        {(step.capture ?? []).length > 0 && (
          <Row label="Captures">
            <Mono>
              {(step.capture ?? []).map((c) => `${c.name} ← ${c.from} ${c.path ?? c.pattern ?? c.header ?? c.cookie ?? ""}`).join(" · ")}
            </Mono>
          </Row>
        )}
        {step.retry && (
          <Row label="Retry">
            {step.retry.attempts} attempts, {step.retry.backoff_s ?? 1}s backoff (timeouts, 5xx, 429 only)
          </Row>
        )}
        {step.expect && (
          <Row label="Checks">
            <Mono>{Object.entries(step.expect).map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")}</Mono>
          </Row>
        )}
        {step.when && (
          <Row label="Only when">
            <Mono>{JSON.stringify(step.when)}</Mono>
          </Row>
        )}
        {step.on_failure && (
          <Row label="On failure">
            {typeof step.on_failure === "string" ? step.on_failure : <>go to <Mono>{step.on_failure.goto}</Mono></>}
          </Row>
        )}
        {step.type === "any_of" && (
          <div className="space-y-2 pt-1">
            <div className="text-xs text-subtle">The first alternative that works wins:</div>
            {(step.alternatives ?? []).map((a, i) => (
              <StepCard key={i} step={a} alt />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ one plan

function mappingSource(v: Json): ReactNode {
  if (typeof v === "string") return <Mono>{v}</Mono>;
  if (Array.isArray(v)) return <Mono>{v.join(" + ")}</Mono>;
  if (v && typeof v === "object") {
    const d = v as Dict;
    if (typeof d.template === "string") return <Mono><Templated text={d.template} /></Mono>;
    if (Array.isArray(d.first_of)) {
      const names = (d.first_of as Json[]).map((m) =>
        typeof m === "string" ? m : typeof (m as Dict)?.template === "string" ? String((m as Dict).template) : JSON.stringify(m),
      );
      return <Mono>first of {names.join(", ")}</Mono>;
    }
  }
  return <Mono>{JSON.stringify(v)}</Mono>;
}

function PlanCard({ seed, plan }: { seed: string; plan: Plan }) {
  const [raw, setRaw] = useState(false);
  const json = JSON.stringify(plan, null, 2);
  const mapping = Object.entries(plan.mapping ?? {});
  const navigate = plan.entry?.navigate ?? [];
  const aiSteps = (plan.steps ?? []).filter((s) => s.type === "ai" || s.type === "captcha").length;
  return (
    <div className="space-y-3 rounded-lg border border-border p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Recipe</div>
          <div className="truncate text-xs text-muted">from {seed}</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {aiSteps === 0 ? <Badge tone="success">runs without AI</Badge> : <Badge tone="warning">{aiSteps} AI step(s)</Badge>}
          {plan.learned_by && <Badge>mapped by {plan.learned_by}</Badge>}
          <Button size="sm" variant="ghost" onClick={() => setRaw(!raw)} aria-expanded={raw}>
            {raw ? "Readable" : "Raw JSON"}
          </Button>
          <CopyButton value={json} label="Copy JSON" />
        </div>
      </div>

      {raw ? (
        <pre className="max-h-[28rem] overflow-auto rounded bg-surface-2 p-3 font-mono text-xs">{json}</pre>
      ) : (
        <>
          {plan.entry?.direct && (
            <div className="space-y-1 rounded-md bg-surface-2/60 p-3">
              <Row label="Starts at">
                <Mono>{plan.entry.direct}</Mono>
              </Row>
              {navigate.length > 0 && (
                <Row label="If it moved">
                  from <Mono>{plan.entry.root}</Mono>, follow{" "}
                  {navigate.map((h, i) => (
                    <span key={i}>
                      {i > 0 && " → "}
                      the link {[...(h.link?.text ?? []).map((t) => `“${t}”`), ...(h.link?.href_contains ?? []).map((t) => `to ${t}`)].join(" or ")}
                    </span>
                  ))}
                </Row>
              )}
            </div>
          )}
          <ol className="space-y-2">
            {(plan.steps ?? []).map((s, i) => (
              <li key={s.id ?? i}>
                <StepCard step={s} index={i} />
              </li>
            ))}
          </ol>
          {mapping.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-border text-subtle">
                    <th className="py-1.5 pr-3 font-medium">Field</th>
                    <th className="py-1.5 font-medium">From</th>
                  </tr>
                </thead>
                <tbody>
                  {mapping.map(([f, v]) => (
                    <tr key={f} className="border-b border-border/60 last:border-0">
                      <td className="py-1.5 pr-3 font-medium">{f}</td>
                      <td className="py-1.5">{mappingSource(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {plan.link_template && (
            <Row label="Item link">
              <Mono>
                <Templated text={plan.link_template} />
              </Mono>
            </Row>
          )}
          {Object.entries(plan.placeholders ?? {}).map(([field, urls]) => (
            <Row key={field} label="Stand-ins">
              {field}: treated as none when it is <Mono>{urls.join(", ")}</Mono>
            </Row>
          ))}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ public

export function RecipeView({ snapshot }: { snapshot: Record<string, unknown> | null | undefined }) {
  const s = snapshot as Snapshot | null | undefined;
  if (!s) return null;
  if (s.implementation === "code") {
    return (
      <div className="px-4 py-3 text-sm">
        Code connector: <Mono>{[s.connector, s.action].filter(Boolean).join(" → ")}</Mono>
        <p className="mt-1 text-xs text-muted">Reviewed and tested code in the scraper0 repository; it has no recipe to show.</p>
      </div>
    );
  }
  const plans = s.plans ?? [];
  if (plans.length === 0) return null;
  return (
    <div className="space-y-3 px-4 py-3">
      {s.learn && (
        <p className="text-xs text-muted">
          Learning used {s.learn.ai_calls ?? 0} AI call(s).
          {(s.learn.notes ?? []).length > 0 && ` Notes: ${(s.learn.notes ?? []).slice(0, 3).join("; ")}`}
        </p>
      )}
      {plans.map((p) => (
        <PlanCard key={p.seed} seed={p.seed} plan={p.plan} />
      ))}
      {(s.check_run ?? []).length > 0 && <PlanRuns runs={s.check_run ?? []} title="Check run (no AI, no browser)" />}
    </div>
  );
}

/** What a plan run actually did: the path taken, retries, failed checks and AI per step. */
export function PlanRuns({ runs, title = "How the recipe ran" }: { runs: RunStats[]; title?: string }) {
  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-subtle">{title}</div>
      {runs.map((r, i) => {
        const fragile =
          r.entry === "navigate" || Object.keys(r.alternatives ?? {}).length > 0 || Object.keys(r.retries ?? {}).length > 0 ||
          (r.expect_failures ?? []).length > 0 || (r.truncated ?? []).length > 0;
        return (
          <div key={i} className="space-y-1 rounded-md border border-border p-3 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate font-medium">{r.seed}</span>
              {fragile ? <Badge tone="warning">took a fallback</Badge> : <Badge tone="success">direct path</Badge>}
              <Badge>{r.requests ?? 0} request(s)</Badge>
              <Badge>{r.items ?? 0} item(s)</Badge>
              <Badge tone={(r.ai_calls ?? 0) > 0 ? "warning" : "neutral"}>
                {r.ai_calls ?? 0} AI call(s){(r.ai_tokens ?? 0) > 0 ? ` · ${r.ai_tokens} tokens` : ""}
              </Badge>
            </div>
            {r.entry === "navigate" && (
              <Row label="Entry">
                the direct URL failed; found the list at <Mono>{r.entry_changed_to}</Mono>
              </Row>
            )}
            {Object.entries(r.alternatives ?? {}).map(([k, v]) => (
              <Row key={k} label="Alternative">
                <Mono>{k}</Mono>: #{v + 1} worked
              </Row>
            ))}
            {Object.entries(r.retries ?? {}).map(([k, v]) => (
              <Row key={k} label="Retries">
                <Mono>{k}</Mono>: {v}
              </Row>
            ))}
            {(r.truncated ?? []).map((t, j) => (
              <Row key={`t${j}`} label="Cut short">
                <Mono>{t.step}</Mono>: stopped by the {t.cap.replace("max_", "")} cap at {t.items} item(s)
                {t.total ? ` of ${t.total}` : ""}
              </Row>
            ))}
            {Object.entries(r.paging ?? {}).map(([k, v]) => (
              <Row key={`p${k}`} label="Unstable order">
                <Mono>{k}</Mono>: {v.unique} unique item(s){v.total ? ` of ${v.total}` : ""} after {v.sweeps} pass(es)
              </Row>
            ))}
            {(r.expect_failures ?? []).map((f, j) => (
              <Row key={j} label="Failed check">
                <Mono>{f.step}</Mono>: {f.reason}
              </Row>
            ))}
            {Object.entries(r.ai_by_step ?? {}).map(([k, v]) => (
              <Row key={k} label="AI">
                <Mono>{k}</Mono>: {v.calls} call(s), {v.tokens} tokens
              </Row>
            ))}
            {r.outcomes && (
              <Row label="Steps">
                <Mono>{Object.entries(r.outcomes).map(([k, v]) => `${k}: ${v}`).join(" · ")}</Mono>
              </Row>
            )}
          </div>
        );
      })}
    </div>
  );
}
