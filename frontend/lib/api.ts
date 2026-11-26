/**
 * Typed client for the scraper0 HTTP API (spec/api.md v1, spec/api-v2.md v2).
 *
 * The browser calls same-origin /v1/* which Next.js rewrites to the API, so the
 * s0_session cookie is first-party. Every request sends credentials, the CSRF
 * header and the selected tenant.
 */

// ---------------------------------------------------------------------------
// Types mirroring spec/api.md
// ---------------------------------------------------------------------------

export type ISODate = string;

export interface User {
  id: string;
  email: string;
  name: string;
  is_platform_admin: boolean;
}

export type TenantRole = "admin" | "member";

export interface TenantMembership {
  id: string;
  slug: string;
  name: string;
  role: TenantRole;
}

export interface Me {
  user: User;
  tenants: TenantMembership[];
}

export type TenantStatus = "active" | "suspended" | string;

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  status: TenantStatus;
  monthly_ai_budget_usd: number | null;
  ai_spend_this_month_usd: number;
  /** Shared-provider spend; the monthly budget caps this. BYO spend is uncapped. */
  ai_shared_spend_this_month_usd?: number;
}

export interface Member {
  user_id: string;
  email: string;
  name: string;
  role: TenantRole;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: ISODate;
  last_used_at: ISODate | null;
  revoked_at: ISODate | null;
}

export interface CreatedApiKey {
  id: string;
  name: string;
  prefix: string;
  key: string;
}

export type ProviderKind = "groq" | "anthropic" | "openai_compat";
export const PROVIDER_KINDS: ProviderKind[] = ["groq", "anthropic", "openai_compat"];

export interface ProviderPublic {
  id: string;
  name: string;
  kind: ProviderKind;
  model: string;
  vision_model: string | null;
  base_url: string | null;
  enabled: boolean;
  is_default: boolean;
  price_in_per_mtok: number | null;
  price_out_per_mtok: number | null;
  key_hint: string | null;
  scope: "shared" | "tenant";
}

export interface ProviderCreate {
  name: string;
  kind: ProviderKind;
  model: string;
  vision_model?: string;
  base_url?: string;
  api_key: string;
  price_in_per_mtok?: number;
  price_out_per_mtok?: number;
}

export type ProviderPatch = Partial<Omit<ProviderCreate, "kind">> & {
  enabled?: boolean;
  is_default?: boolean;
};

export interface ProvidersOverview {
  shared: ProviderPublic[];
  own: ProviderPublic[];
  active: ProviderPublic | null;
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms: number | null;
  error?: string;
}

export interface UsageDay {
  day: string;
  usd: number;
  calls: number;
}

export interface UsagePurpose {
  purpose: string;
  usd: number;
  calls: number;
}

export interface TenantUsage {
  total_usd: number;
  by_day: UsageDay[];
  by_purpose: UsagePurpose[];
  budget_usd: number | null;
}

export type FieldType = "text" | "datetime" | "url" | "image" | "number" | "bool" | "enum" | "address";
export const FIELD_TYPES: FieldType[] = ["text", "datetime", "url", "image", "number", "bool", "enum", "address"];

export interface EntityField {
  name: string;
  type: FieldType;
  required?: boolean;
  description?: string;
  choices?: string[];
  weight?: number;
}

export interface EntitySpec {
  name: string;
  description: string;
  fields: EntityField[];
  version: number;
  builtin: boolean;
}

export interface Recipe {
  id: string;
  domain: string;
  path_pattern: string;
  entity: string;
  rules: unknown;
  sample_url: string | null;
  successes: number;
  failures: number;
  status: string;
  updated_at: ISODate;
}

/** `actions` is not typed in the spec; we accept plain names or {name, description}. */
export type ConnectorAction = string | { name: string; description?: string };

export interface Connector {
  name: string;
  source: string;
  description: string;
  actions: ConnectorAction[];
  needs_browser: boolean;
}

export function actionName(a: ConnectorAction): string {
  return typeof a === "string" ? a : a.name;
}

export type JobKind = "crawl" | "connector" | "ingest";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export const JOB_STATUSES: JobStatus[] = ["queued", "running", "succeeded", "failed", "cancelled"];
export const TERMINAL_STATUSES: JobStatus[] = ["succeeded", "failed", "cancelled"];

export interface CrawlOptions {
  max_detail_pages?: number;
  max_list_pages?: number;
  allow_llm?: boolean;
  learn?: boolean;
  use_cache?: boolean;
  event_filter?: string;
  category_filter?: string;
  include_past?: boolean;
  include_online?: boolean;
  require_address?: boolean;
  portal?: string;
  /** Regex on URL paths of detail pages (non-event entities). */
  link_pattern?: string;
}

export interface CrawlInput {
  entity: string | { name: string; description?: string; fields: EntityField[] };
  urls: string[];
  options?: CrawlOptions;
}

export interface ConnectorInput {
  connector: string;
  action: string;
  data: unknown;
  auth?: unknown;
}

export type JobCreate =
  | { kind: "crawl"; input: CrawlInput; wait?: boolean }
  | { kind: "connector"; input: ConnectorInput; wait?: boolean };

export interface JobProgress {
  message?: string | null;
  pages?: number;
  items?: number;
  llm_calls?: number;
  recipes_learned?: number;
  [k: string]: unknown;
}

export interface StoredCounts {
  created: number;
  updated: number;
  unchanged: number;
}

export interface AiResultInfo {
  calls?: number;
  cost_usd?: number;
  errors?: string[];
}

export interface IngestReportEntry {
  input: string;
  events: number;
  error: string | null;
}

/** result for crawl / ingest jobs. */
export interface CrawlResult {
  entity?: string;
  items: Array<Record<string, unknown>>;
  stats: Record<string, unknown>;
  stored?: StoredCounts;
  ai?: AiResultInfo;
  /** ingest only */
  report?: IngestReportEntry[];
  /** ingest only */
  needs_review?: number;
}

/** result for connector jobs. */
export interface ConnectorResult {
  data: unknown;
  stored?: StoredCounts;
  ai?: AiResultInfo;
}

export interface Job {
  id: string;
  kind: JobKind;
  status: JobStatus;
  input: unknown;
  result: CrawlResult | ConnectorResult | null;
  error: string | { code?: string; message?: string } | null;
  progress: JobProgress | null;
  source_id: string | null;
  created_at: ISODate;
  started_at: ISODate | null;
  finished_at: ISODate | null;
}

/** Not spelled out in the spec: assumed to be Job without input/result. */
export type JobSummary = Omit<Job, "input" | "result"> & Partial<Pick<Job, "input">>;

export interface Source {
  id: string;
  name: string;
  kind: "crawl" | "connector";
  config: CrawlInput | ConnectorInput;
  interval_minutes: number;
  enabled: boolean;
  next_run_at: ISODate | null;
  last_job: JobSummary | null;
}

export interface SourceCreate {
  name: string;
  kind: "crawl" | "connector";
  config: CrawlInput | ConnectorInput;
  interval_minutes: number;
  enabled?: boolean;
}

export interface Item {
  id: string;
  entity: string;
  item_key: string;
  data: Record<string, unknown>;
  source_id: string | null;
  first_seen_at: ISODate;
  last_seen_at: ISODate;
  changed_at: ISODate;
}

export interface AdminTenant extends Tenant {
  members: number;
  created_at: ISODate;
}

export interface AccessRequest {
  id: string;
  name: string;
  email: string;
  organisation: string | null;
  use_case: string | null;
  status: "pending" | "approved" | "rejected" | string;
  created_at: ISODate;
}

export interface AdminUsage {
  total_usd: number;
  by_tenant: Array<{ tenant_id: string; slug: string; usd: number; calls: number }>;
  by_day: UsageDay[];
}

// ---------------------------------------------------------------------------
// v2: sites and actions (spec/api-v2.md)
// ---------------------------------------------------------------------------

export type ActionKind = "read" | "write";
export type ActionImplementation = "learned" | "code";
export type ActionStatus = "draft" | "locked" | "broken";
export type VersionStatus = "draft" | "locked" | "retired" | "broken";

/** JSON Schema objects; rendered as JSON, never interpreted beyond a peek at `properties`. */
export type JsonSchema = Record<string, unknown>;

export interface ActionSummary {
  name: string;
  kind: ActionKind;
  status: ActionStatus;
}

export interface SiteSummary {
  site_id: string;
  base_url: string;
  name: string;
  actions: ActionSummary[];
}

export interface Action {
  site_id: string;
  name: string;
  kind: ActionKind;
  /** Code actions such as lookups and writes may have no entity. */
  entity: string | null;
  implementation: ActionImplementation;
  seed_urls: string[];
  status: ActionStatus;
  locked_version: number | null;
  input_schema: JsonSchema | null;
  output_schema: JsonSchema | null;
  last_success_at: ISODate | null;
  last_failure_at: ISODate | null;
  consecutive_failures: number;
}

export interface Site extends Omit<SiteSummary, "actions"> {
  created_at: ISODate;
  created_by_tenant: string | null;
  actions: Action[];
}

export interface VersionPreview {
  /** First 20 items. Either bare entity data or stored items ({id, updated_at, data}). */
  items: Array<Record<string, unknown>>;
  count: number;
  checked_at: ISODate | null;
}

export interface ActionVersion {
  version: number;
  status: VersionStatus;
  created_at: ISODate;
  locked_at: ISODate | null;
  locked_by: string | null;
  preview: VersionPreview | null;
  implementation_snapshot: Record<string, unknown> | null;
}

export interface ActionContract {
  site_id: string;
  action: string;
  kind: ActionKind;
  version: number;
  input_schema: JsonSchema | null;
  output_schema: JsonSchema | null;
  endpoint: string;
  curl: string;
}

export interface ActionCreate {
  name: string;
  kind: ActionKind;
  entity: string;
  seed_urls?: string[];
}

export interface LearnStarted {
  job_id: string;
  version: number;
}

export interface ReadOptions {
  event_filter?: string;
  category_filter?: string;
  categories?: string[];
  include_online?: boolean;
  /** YYYY-MM-DD; default today (Asia/Kolkata). */
  from?: string;
}

export interface ReadRequest {
  options?: ReadOptions;
  freshness_hours?: number;
  timeout_s?: number;
}

export interface ReadItem {
  id: string;
  updated_at: ISODate;
  data: Record<string, unknown>;
}

export interface ReadResult {
  site_id: string;
  action: string;
  version: number;
  items: ReadItem[];
  complete: boolean;
  scraped_at: ISODate | null;
  refresh_job_id: string | null;
  health: "ok" | "broken";
  /** Set when the AI filter could not run (nothing was filtered out). */
  ai_error?: string | null;
}

export interface LookupRequest {
  input: Record<string, unknown>;
  auth?: Record<string, unknown>;
  timeout_s?: number;
}

/** 200 → output; 202 (timed out) → only job_id. */
export type LookupResult =
  | { site_id: string; action: string; version: number; output: unknown }
  | { job_id: string };

export interface WriteRequest {
  input: Record<string, unknown>;
  auth?: Record<string, unknown>;
  force?: boolean;
  timeout_s?: number;
}

export type WriteResult =
  | { status: "succeeded"; output: unknown; idempotency_key: string; job_id: string; replayed: boolean }
  | { status: "running"; idempotency_key: string; job_id: string };

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

const STATUS_TEXT: Record<number, string> = {
  400: "The request was invalid.",
  401: "You need to sign in.",
  403: "You don't have permission to do that.",
  404: "Not found.",
  409: "That conflicts with something that already exists.",
  413: "The upload is too large.",
  422: "Some fields are invalid.",
  429: "Too many requests. Try again later.",
  502: "The upstream service failed.",
};


/** Friendly wording for the v2 error codes (spec/api-v2.md). */
const V2_MESSAGES: Record<string, (serverMessage: string) => string> = {
  site_not_onboarded: () => "This site hasn't been onboarded yet. Onboard it from the Sites page first.",
  action_not_available: () =>
    "This action has no locked version yet. Learn it, check the preview and lock a version first.",
  action_exists: () => "This site already has an action with that name. Use the existing one or pick another name.",
  invalid_input: (m) => `Some of the input is invalid${m ? `: ${m}` : "."}`,
  action_failed: (m) => `The action failed on the site${m ? `: ${m}` : "."}`,
  outcome_unknown: (m) =>
    `The outcome is unknown: it may have been submitted. Check before retrying${m ? ` (${m})` : ""}.`,
  forbidden: () => "Only tenant admins can do that.",
};

/** Rethrow an ApiError with a friendly v2 message; the code and status are kept. */
function friendlyV2(err: unknown): never {
  if (err instanceof ApiError) {
    const fn = V2_MESSAGES[err.code];
    const generic = STATUS_TEXT[err.status] === err.message;
    if (fn) throw new ApiError(err.status, err.code, fn(generic ? "" : err.message));
  }
  throw err;
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = `http_${res.status}`;
  let message = STATUS_TEXT[res.status] ?? `Request failed (${res.status}).`;
  try {
    const body: unknown = await res.json();
    const e = (body as { error?: { code?: unknown; message?: unknown } } | null)?.error;
    if (e && typeof e === "object") {
      if (typeof e.code === "string") code = e.code;
      if (typeof e.message === "string" && e.message) message = e.message;
    }
  } catch {
    // non-JSON body (e.g. proxy error page) — keep defaults
  }
  if (res.status === 502 && code === "http_502") message = "The API is unreachable right now.";
  return new ApiError(res.status, code, message);
}

// ---------------------------------------------------------------------------
// Tenant selection (persisted in localStorage)
// ---------------------------------------------------------------------------

const TENANT_KEY = "s0_tenant";

export function getStoredTenant(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TENANT_KEY);
  } catch {
    return null;
  }
}

export function setStoredTenant(id: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (id) window.localStorage.setItem(TENANT_KEY, id);
    else window.localStorage.removeItem(TENANT_KEY);
  } catch {
    // storage unavailable — selection lasts for this page only
  }
  currentTenant = id;
}

let currentTenant: string | null = null;

function tenantHeader(): string | null {
  return currentTenant ?? getStoredTenant();
}

// ---------------------------------------------------------------------------
// Core request
// ---------------------------------------------------------------------------

type Query = Record<string, string | number | boolean | null | undefined>;

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Query;
  /** Skip the X-Tenant header (auth and public endpoints). */
  noTenant?: boolean;
  signal?: AbortSignal;
  /** Extra request headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Requested-With": "scraper0",
    ...opts.headers,
  };
  const tenant = opts.noTenant ? null : tenantHeader();
  if (tenant) headers["X-Tenant"] = tenant;

  let body: BodyInit | undefined;
  if (opts.body instanceof FormData) {
    body = opts.body; // browser sets the multipart boundary
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(withQuery(path, opts.query), {
      method,
      headers,
      body,
      credentials: "include",
      cache: "no-store",
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Could not reach the server. Check your connection.");
  }

  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(res.status, "invalid_response", "The server sent an unreadable response.");
  }
}

type Envelope<T> = { data: T };

async function data<T>(path: string, opts?: RequestOptions): Promise<T> {
  const res = await request<Envelope<T>>(path, opts);
  return res?.data as T;
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const api = {
  // Public
  health: () => request<{ status: string; message: string; db: string; version: string }>("/api/health", { noTenant: true }),
  meta: () =>
    data<{ version: string; entities: string[]; connectors: Connector[] }>("/v1/meta", { noTenant: true }),
  requestAccess: (body: { name: string; email: string; organisation?: string; use_case?: string }) =>
    data<{ id: string; status: "pending" }>("/v1/access-requests", { method: "POST", body, noTenant: true }),

  // Auth
  login: (email: string, password: string) =>
    data<{ user: User }>("/v1/auth/login", { method: "POST", body: { email, password }, noTenant: true }),
  logout: () => request<void>("/v1/auth/logout", { method: "POST", noTenant: true }),
  me: (signal?: AbortSignal) => data<Me>("/v1/auth/me", { noTenant: true, signal }),
  acceptInvite: (token: string, name: string, password: string) =>
    data<{ user: User }>("/v1/auth/accept-invite", {
      method: "POST",
      body: { token, name, password },
      noTenant: true,
    }),

  changePassword: (current_password: string, new_password: string) =>
    data<{ ok: boolean }>("/v1/auth/password", { method: "POST", body: { current_password, new_password }, noTenant: true }),

  // Tenant
  tenant: () => data<Tenant>("/v1/tenant"),
  members: () => data<Member[]>("/v1/members"),
  inviteMember: (email: string, role: TenantRole) =>
    data<{ invite_id: string; invite_url: string }>("/v1/members/invite", { method: "POST", body: { email, role } }),
  removeMember: (userId: string) => request<void>(`/v1/members/${enc(userId)}`, { method: "DELETE" }),

  apiKeys: () => data<ApiKey[]>("/v1/api-keys"),
  createApiKey: (name: string) => data<CreatedApiKey>("/v1/api-keys", { method: "POST", body: { name } }),
  revokeApiKey: (id: string) => request<void>(`/v1/api-keys/${enc(id)}`, { method: "DELETE" }),

  providers: () => data<ProvidersOverview>("/v1/ai/providers"),
  createProvider: (body: ProviderCreate) => data<ProviderPublic>("/v1/ai/providers", { method: "POST", body }),
  updateProvider: (id: string, body: ProviderPatch) =>
    data<ProviderPublic>(`/v1/ai/providers/${enc(id)}`, { method: "PATCH", body }),
  deleteProvider: (id: string) => request<void>(`/v1/ai/providers/${enc(id)}`, { method: "DELETE" }),
  testProvider: (id: string) => data<ProviderTestResult>(`/v1/ai/providers/${enc(id)}/test`, { method: "POST" }),
  usage: (days = 30) => data<TenantUsage>("/v1/ai/usage", { query: { days } }),

  entities: () => data<EntitySpec[]>("/v1/entities"),
  saveEntity: (name: string, body: { description: string; fields: EntityField[] }) =>
    data<EntitySpec>(`/v1/entities/${enc(name)}`, { method: "PUT", body }),
  deleteEntity: (name: string) => request<void>(`/v1/entities/${enc(name)}`, { method: "DELETE" }),

  recipes: (query?: { domain?: string; entity?: string }) => data<Recipe[]>("/v1/recipes", { query }),
  deleteRecipe: (id: string) => request<void>(`/v1/recipes/${enc(id)}`, { method: "DELETE" }),

  connectors: () => data<Connector[]>("/v1/connectors"),

  // Jobs
  createJob: (body: JobCreate) => data<Job>("/v1/jobs", { method: "POST", body }),
  ingest: (form: FormData) => data<Job>("/v1/jobs/ingest", { method: "POST", body: form }),
  jobs: (query?: { status?: string; kind?: string; limit?: number; before?: string }) =>
    data<JobSummary[]>("/v1/jobs", { query }),
  job: (id: string, signal?: AbortSignal) => data<Job>(`/v1/jobs/${enc(id)}`, { signal }),
  cancelJob: (id: string) => data<Job>(`/v1/jobs/${enc(id)}/cancel`, { method: "POST" }),
  /**
   * EventSource cannot send headers, so the selected tenant also travels as a
   * `tenant` query parameter (the API should accept it on this endpoint).
   */
  jobEventsUrl: (id: string) => withQuery(`/v1/jobs/${enc(id)}/events`, { tenant: tenantHeader() }),

  // Sources
  sources: () => data<Source[]>("/v1/sources"),
  createSource: (body: SourceCreate) => data<Source>("/v1/sources", { method: "POST", body }),
  updateSource: (id: string, body: Partial<SourceCreate>) =>
    data<Source>(`/v1/sources/${enc(id)}`, { method: "PATCH", body }),
  deleteSource: (id: string) => request<void>(`/v1/sources/${enc(id)}`, { method: "DELETE" }),
  runSource: (id: string) => data<Job>(`/v1/sources/${enc(id)}/run`, { method: "POST" }),

  // Items
  items: async (query: { entity?: string; q?: string; limit?: number; offset?: number }) => {
    const res = await request<{ data: Item[]; total: number }>("/v1/items", { query });
    return { items: res.data ?? [], total: res.total ?? 0 };
  },

  // Platform admin
  admin: {
    tenants: () => data<AdminTenant[]>("/v1/admin/tenants", { noTenant: true }),
    createTenant: (body: { slug: string; name: string; monthly_ai_budget_usd?: number; admin_email?: string }) =>
      data<{ tenant: AdminTenant; invite_url?: string | null }>("/v1/admin/tenants", {
        method: "POST",
        body,
        noTenant: true,
      }),
    updateTenant: (id: string, body: { name?: string; status?: string; monthly_ai_budget_usd?: number | null }) =>
      data<AdminTenant>(`/v1/admin/tenants/${enc(id)}`, { method: "PATCH", body, noTenant: true }),
    accessRequests: (status = "pending") =>
      data<AccessRequest[]>("/v1/admin/access-requests", { query: { status }, noTenant: true }),
    approveAccessRequest: (id: string, body: { slug: string; name?: string; monthly_ai_budget_usd?: number }) =>
      data<{ tenant: AdminTenant; invite_url: string }>(`/v1/admin/access-requests/${enc(id)}/approve`, {
        method: "POST",
        body,
        noTenant: true,
      }),
    inviteToTenant: (id: string, email: string, role: TenantRole = "admin") =>
      data<{ invite_url: string }>(`/v1/admin/tenants/${enc(id)}/invite`, {
        method: "POST",
        body: { email, role },
        noTenant: true,
      }),
    rejectAccessRequest: (id: string) =>
      request<unknown>(`/v1/admin/access-requests/${enc(id)}/reject`, { method: "POST", noTenant: true }),
    providers: () => data<ProviderPublic[]>("/v1/admin/ai/providers", { noTenant: true }),
    createProvider: (body: ProviderCreate) =>
      data<ProviderPublic>("/v1/admin/ai/providers", { method: "POST", body, noTenant: true }),
    updateProvider: (id: string, body: ProviderPatch) =>
      data<ProviderPublic>(`/v1/admin/ai/providers/${enc(id)}`, { method: "PATCH", body, noTenant: true }),
    deleteProvider: (id: string) =>
      request<void>(`/v1/admin/ai/providers/${enc(id)}`, { method: "DELETE", noTenant: true }),
    testProvider: (id: string) =>
      data<ProviderTestResult>(`/v1/admin/ai/providers/${enc(id)}/test`, { method: "POST", noTenant: true }),
    usage: (days = 30) => data<AdminUsage>("/v1/admin/usage", { query: { days }, noTenant: true }),
  },
};

// v2 responses keep their codes; messages are made friendly (see V2_MESSAGES).
const v2 = <T>(path: string, opts?: RequestOptions) => data<T>(path, opts).catch(friendlyV2);
const actionPath = (siteId: string, name: string) => `/v2/sites/${enc(siteId)}/actions/${enc(name)}`;

export const sitesApi = {
  list: (q?: string) => v2<SiteSummary[]>("/v2/sites", { query: { q } }),
  /** `existing: true` means another tenant (or the platform) onboarded it already. */
  onboard: async (base_url: string) => {
    const res = await request<{ data: Site; existing?: boolean }>("/v2/sites", {
      method: "POST",
      body: { base_url },
    }).catch(friendlyV2);
    return { site: res.data, existing: !!res.existing };
  },
  get: (siteId: string) => v2<Site>(`/v2/sites/${enc(siteId)}`),

  createAction: (siteId: string, body: ActionCreate) =>
    v2<Action>(`/v2/sites/${enc(siteId)}/actions`, { method: "POST", body }),
  action: (siteId: string, name: string) => v2<Action>(actionPath(siteId, name)),
  learn: (siteId: string, name: string) => v2<LearnStarted>(`${actionPath(siteId, name)}/learn`, { method: "POST" }),
  repair: (siteId: string, name: string) => v2<LearnStarted>(`${actionPath(siteId, name)}/repair`, { method: "POST" }),
  versions: (siteId: string, name: string) => v2<ActionVersion[]>(`${actionPath(siteId, name)}/versions`),
  lock: (siteId: string, name: string, version: number) =>
    v2<ActionContract>(`${actionPath(siteId, name)}/versions/${enc(String(version))}/lock`, { method: "POST" }),
  contract: (siteId: string, name: string) => v2<ActionContract>(`${actionPath(siteId, name)}/contract`),

  read: (siteId: string, name: string, body: ReadRequest = {}) =>
    v2<ReadResult>(`${actionPath(siteId, name)}/read`, { method: "POST", body }),
  lookup: (siteId: string, name: string, body: LookupRequest) =>
    v2<LookupResult>(`${actionPath(siteId, name)}/read`, { method: "POST", body }),
  /** Not used by the dashboard (writes are code-only and run by callers); here for completeness. */
  write: (siteId: string, name: string, body: WriteRequest, idempotencyKey?: string) =>
    v2<WriteResult>(`${actionPath(siteId, name)}/write`, { method: "POST", body, headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined }),
};
