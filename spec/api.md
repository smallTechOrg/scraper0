# scraper0 HTTP API (v0.1)

JSON everywhere, apart from SSE streams. Times are ISO-8601 UTC. Errors use the shape
`{"error": {"code": "snake_case", "message": "human text"}}` with 400 / 401 / 403 / 404 / 409 / 413 / 422 / 429 / 502 status codes.
The one exception is the legacy endpoint, which keeps its old error shape (see below).

## Authentication

| Caller | How | Scope |
|---|---|---|
| Programs (hashtaglocal-backend, scripts) | `X-API-Key: s0_…` header (tenant API key) | that tenant |
| Dashboard users | `s0_session` HTTP-only cookie from `POST /v1/auth/login` | user's tenants; platform admin sees all |
| Cloud Tasks / Scheduler | `Authorization: Bearer <SCRAPER0_INTERNAL_TOKEN>` | `/internal/*` only |

When a session user belongs to several tenants, they pick one with the `X-Tenant: <tenant_id>` header. Without the header, their first tenant is used.
State-changing session requests must send the `X-Requested-With: scraper0` header (CSRF guard). API-key requests are exempt.
Roles: `platform_admin` (user flag), and per tenant `admin` or `member`. Members can read everything in their tenant and run jobs.
Only admins manage keys, sources, providers and members.

## Public

```
GET  /api/health                          → {"status":"ok","message":"Service is running","db":"ok","version":"0.1.0"}   (legacy shape kept)
POST /v1/access-requests                  {name, email, organisation?, use_case?} → 201 {"data": {"id","status":"pending"}}
                                          (rate-limited per IP: 5/hour)
GET  /v1/meta                             → {"data": {"version", "entities": [builtin names], "connectors": [{name, source, description, actions}]}}
```

## Auth

```
POST /v1/auth/login        {email, password} → 200 {"data": {"user": User}} + Set-Cookie s0_session   | 401 invalid_credentials
POST /v1/auth/logout       → 204
GET  /v1/auth/me           → {"data": {"user": User, "tenants": [{id, slug, name, role}]}}           | 401
POST /v1/auth/password    {current_password, new_password} → {"data": {"ok": true}}  (ends all other sessions; new cookie)
POST /v1/auth/accept-invite {token, name, password} → {"data": {"user": User}} + cookie            | 400 invalid_invite
User = {id, email, name, is_platform_admin}
```

## Tenant (session or API key; tenant from key or X-Tenant)

```
GET    /v1/tenant                         → {"data": {id, slug, name, status, monthly_ai_budget_usd, ai_spend_this_month_usd,
                                                     ai_shared_spend_this_month_usd}}   (the budget caps shared spend; BYO spend is uncapped)
GET    /v1/members                        → {"data": [{user_id, email, name, role}]}
POST   /v1/members/invite   (admin)       {email, role} → {"data": {invite_id, invite_url}}   (invite_url = FRONTEND_URL/invite/<token>)
DELETE /v1/members/{user_id} (admin)      → 204

GET    /v1/api-keys                       → {"data": [{id, name, prefix, created_at, last_used_at, revoked_at}]}
POST   /v1/api-keys        (admin)        {name} → 201 {"data": {id, name, prefix, key}}   (key shown once)
DELETE /v1/api-keys/{id}   (admin)        → 204 (revoke)

GET    /v1/ai/providers                   → {"data": {"shared": [ProviderPublic], "own": [ProviderPublic], "active": ProviderPublic|null}}
POST   /v1/ai/providers    (admin)        {name, kind, model, vision_model?, base_url?, api_key, price_in_per_mtok?, price_out_per_mtok?} → 201  (BYO key)
PATCH  /v1/ai/providers/{id} (admin)      {enabled?, is_default?, model?, vision_model?, api_key?, …} → 200
DELETE /v1/ai/providers/{id} (admin)      → 204
POST   /v1/ai/providers/{id}/test (admin) → {"data": {"ok": bool, "latency_ms", "error"?}}
GET    /v1/ai/usage?days=30               → {"data": {"total_usd", "by_day": [{day, usd, calls}], "by_purpose": [{purpose, usd, calls}], "budget_usd"}}
ProviderPublic = {id, name, kind, model, vision_model, base_url, enabled, is_default, price_in_per_mtok, price_out_per_mtok, key_hint ("…ab12"), scope ("shared"|"tenant")}
kind ∈ groq | anthropic | openai_compat

GET    /v1/entities                       → {"data": [EntitySpec]}     (built-in + this tenant's)
PUT    /v1/entities/{name} (admin)        {description, fields:[{name,type,required?,description?,choices?,weight?}]} → {"data": EntitySpec}  (version bumps)
DELETE /v1/entities/{name} (admin)        → 204
EntitySpec = {name, description, fields, version, builtin}
field type ∈ text | datetime | url | image | number | bool | enum | address

GET    /v1/recipes?domain=&entity=        → {"data": [Recipe]}  (built-in entity recipes + this tenant's custom-entity recipes)
DELETE /v1/recipes/{id}    (admin)        → 204   (forget a learned layout; built-in recipes: platform admin only)
Recipe = {id, domain, path_pattern, entity, rules, sample_url, successes, failures, status, updated_at}

GET    /v1/connectors                     → {"data": [{name, source, description, actions, needs_browser}]}
```

## Jobs (history)

There are no ad-hoc jobs. Every scrape runs through an onboarded site and its locked action (v2,
[api-v2.md](api-v2.md)): learning and repair (`site_learn`), refreshes (`site_refresh`: a read past its freshness,
`POST …/refresh`, a schedule) and writes (`site_write`). `connector` jobs run a code connector for a v2 lookup or
the legacy `/api/v1/scrape`. Older `crawl` / `ingest` jobs stay as read-only history.

```
POST /v1/jobs                → 422 invalid_input (points to POST /v2/sites …). Uploads (/v1/jobs/ingest) are gone.
GET  /v1/jobs?status=&kind=&limit=50&before=<created_at>  → {"data": [JobSummary]}
GET  /v1/jobs/{id}           → {"data": Job}
GET  /v1/jobs/{id}/events    → text/event-stream; events: `progress` {message, ...}, `log`, `done` {status}; resumes with Last-Event-ID
POST /v1/jobs/{id}/cancel    → {"data": Job}   (cooperative; running jobs stop at the next page)
Job = {id, kind, status, input, result, error, progress: {message, pages, items, llm_calls, recipes_learned}, source_id,
       site_id, action, created_at, started_at, finished_at}
kind ∈ site_refresh | site_learn | site_write | connector   (history may also hold crawl | ingest)
status ∈ queued | running | succeeded | failed | cancelled
result (site_refresh) = {site_id, action, version, count, stored: {created, updated, unchanged}, ai}
result (site_learn)   = {site_id, action, version, preview, implementation_snapshot, ai}
result (connector)    = {data: <connector payload>, stored?: {...}, ai: {...}}
```

## Sources (schedules on site actions)

```
GET    /v1/sources                        → {"data": [Source]}
POST   /v1/sources         (admin)        {name, site_id, action (default fetch_events), interval_minutes ≥ 15, enabled?} → 201
                                           404 site_not_onboarded | 404 action_not_available (nothing locked) | 422 (not a list action)
PATCH  /v1/sources/{id}    (admin)        → 200
DELETE /v1/sources/{id}    (admin)        → 204
POST   /v1/sources/{id}/run (admin)       → 202 {"data": Job}   (refresh now; a joined refresh of another tenant → {id: null, kind, status})
Source = {id, name, site_id, action, interval_minutes, enabled, next_run_at, last_job: JobSummary|null}
```
The tick refreshes due schedules (joining a running refresh; a broken action keeps its hourly retry limit).

## Stored results and change feed

There is one item store, shared through sites (migrations `0004`, `0006`):

- **Every item belongs to a site + action** and is shared by every tenant. Every run that scrapes the site updates
  the same rows, whoever starts it: v2 refreshes and code connector runs (the site of the connector's code action,
  e.g. TEAMEVEREST → `teameverest-ngo`). Item `id` = `itm_` + sha256(site_id | action | item_key)[:24], the same id
  v2 reads return. (Private items — uploads — were removed in `0006`.)
- Each run bumps `last_seen_at` on every item it saw; `updated_at` moves only when the content changed (the image URL
  is not content). No versions are kept.

```
GET /v1/items?entity=&site=&action=&q=&sort=last_seen|updated|first_seen&limit=50&offset=0  → {"data": [Item], "total"}
GET /v1/items/sites                                       → {"data": [{site_id, items}]}   (sites holding visible items)
GET /v1/items/changes?entity=event&cursor=0&limit=500     → {"data": [{change: created|updated, item: Item}], "next_cursor": int, "has_more": bool}
Item = {id: "itm_…", entity, item_key, data, site_id, action, private (always false), job_id, first_seen_at, last_seen_at,
        updated_at, changed_at (= updated_at)}
```
Every tenant sees the same items, in both the list and the change feed. Consumers store
`next_cursor` and pass it back as `cursor`, so they only ever see new or changed items. (`0004` restarted the feed:
cursors from before it are not valid.) `source_id` is no longer on items; `job_id` is the last job that saw the item.

## Platform admin (session with is_platform_admin)

```
GET    /v1/admin/tenants                  → {"data": [{id, slug, name, status, monthly_ai_budget_usd, ai_spend_this_month_usd, members, created_at}]}
POST   /v1/admin/tenants                  {slug, name, monthly_ai_budget_usd?, admin_email?} → 201 {"data": {tenant, invite_url?}}
PATCH  /v1/admin/tenants/{id}             {name?, status?, monthly_ai_budget_usd?} → 200
POST   /v1/admin/tenants/{id}/invite      {email, role?: admin|member} → {"data": {invite_url}}
GET    /v1/admin/access-requests?status=pending → {"data": [AccessRequest]}
POST   /v1/admin/access-requests/{id}/approve {slug, name?, monthly_ai_budget_usd?} → {"data": {tenant, invite_url}}
POST   /v1/admin/access-requests/{id}/reject  → 200
GET    /v1/admin/ai/providers             → {"data": [ProviderPublic]}          (shared providers)
POST   /v1/admin/ai/providers             same body as tenant BYO → 201        (shared)
PATCH  /v1/admin/ai/providers/{id}        → 200
DELETE /v1/admin/ai/providers/{id}        → 204
POST   /v1/admin/ai/providers/{id}/test   → {"data": {ok, latency_ms, error?}}
GET    /v1/admin/usage?days=30            → {"data": {"total_usd", "by_tenant": [{tenant_id, slug, usd, calls}], "by_day": [...]}}
```

## Internal (Cloud Tasks / Cloud Scheduler)

```
POST /internal/jobs/{id}/run      runs a queued job to completion inside this request (Cloud Run keeps CPU allocated)
POST /internal/tick               refreshes due schedules (sources); marks jobs whose heartbeat is > 10 min old as failed
```

## Legacy (unchanged contract, used by hashtaglocal-backend)

```
POST /api/v1/scrape    {"source", "context": {"portal", "action": {"type", "data"}, "auth"?}}
                       headers: X-API-Key (hashtaglocal tenant) — or none when SCRAPER0_LEGACY_KEYLESS=true; X-Backend-Url for FETCH_EVENTS
→ 200 {"data": <connector payload>}       e.g. {"data": {"events": [...]}} | {"data": {"tracking_id": 123}} | {"data": {"status", "meta_data"}}
→ 422 {"code": 422, "status": "Unprocessable Entity", "errors": {"json": ["message"]}, "message": "..."}  (same shape as the old flask-smorest ValidationError)
```
This runs synchronously as a job (`wait=true`), so it also appears in the dashboard and its events land in stored results.
