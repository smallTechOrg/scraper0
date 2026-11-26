# scraper0 architecture

```
                 ┌──────────────── Cloud Run: scraper0-web (Next.js) ───────────────┐
 browser ───────▶│ landing · /login · /app (tenant dashboard) · /admin (platform)     │── rewrites /v1, /api ──┐
                 └────────────────────────────────────────────────────────────────────┘                        │
 hashtaglocal-backend ── POST /api/v1/scrape (legacy) ──┐                                                       │
 programs ───────────── X-API-Key ── /v1/* ─────────────┤                                                       ▼
 Cloud Scheduler ────── /internal/tick (1/min) ─────────┤      ┌──────── Cloud Run: scraper0-api (FastAPI) ─────────┐
 Cloud Tasks ────────── /internal/jobs/{id}/run ────────┴─────▶│ api/      routes, auth (key | session), CSRF        │
                                                                │ jobs/     submit → dispatch → run (atomic claim)    │
                                                                │ services/ ai (providers, budget, metering), auth,   │
                                                                │           items (upsert + change feed), entities    │
                                                                │ core/     crawler · recipes · llm · fetch · store   │
                                                                │ events/   event engine, extractors, ingest, dedupe  │
                                                                │ connectors/ TEAMEVEREST MYBHARATGOVIN IVOLUNTEERIN  │
                                                                │            SMARTONEBLR (Playwright) WEBEVENTS       │
                                                                └───────────────┬────────────────────────────────────┘
                                                                                │ ≤ 3 connections
                                                               Cloud SQL (hashtaglocal instance) · database `scraper0`
```

## Concepts

- **Tenant:** an organisation using scraper0. Users join through invites, with role `admin` or `member`. Programs authenticate
  with tenant API keys. Platform admins manage tenants, access requests and shared AI providers.
- **Entity spec:** *what* to extract, described as data: fields with types and required flags. `event` is built in, and
  tenants define their own (jobs, NGOs, …). Changing a spec bumps its version, which invalidates cached results.
- **Recipe:** a learned page layout for (domain, URL path pattern, entity). It holds CSS-selector rules induced from one
  AI reading, verified by re-extracting that same page. Later pages with the layout use selectors only, with no AI.
  After 3 failures in a row the recipe goes stale and is relearned; old selectors are kept as fallbacks.
  **Recipes for built-in entities are shared by all tenants**: a public page is learned once for everyone. Custom
  entities' recipes are private to their tenant (`entity_key = "<tenant_id>:<name>"`).
- **Connector:** hand-written integration for a site that needs it (login, forms, private API). It exposes named
  actions and gets AI and encrypted state through its context, never directly.
- **Job:** every scrape is a job (crawl | connector | ingest) with persisted progress events (served as SSE), result,
  cancellation and heartbeat. **Source:** a saved job input plus an interval, run by the scheduler tick.
- **Item:** a stored result per (tenant, entity, item_key), with first_seen / last_seen / changed_at. Each change appends to
  `item_changes`, and its id is the change-feed cursor.

## Per page, cheapest first

1. **page cache:** if the fingerprint (visible text + JSON-LD + meta + image URLs) is unchanged, return the previous result
2. **structured data** (events): JSON-LD, microdata, ICS, RSS, OpenGraph
3. **learned recipe** for this domain + path pattern
4. **heuristic** (events): labelled text ("Date:", "Venue:")
5. **AI:** the spec-driven prompt returns values and verbatim sources, then a recipe is learned from them

**JavaScript-only pages.** When the static HTML is an empty shell (almost no visible text, but scripts), the page is
rendered in headless Chromium (`core/render.py`: one browser on a dedicated thread; every sub-request passes the SSRF
guard; images/fonts/media are blocked). While it renders, same-site JSON responses to GET requests are captured.
`core/json_extract.py` then finds the array that holds the entity and maps keys to fields: name heuristics first, the
LLM only if those fail. It verifies the mapping on the real items, works out the item link pattern from the rendered
anchors, and resolves `*_id` references into names from other captured arrays. The result is stored as a recipe with
strategy `json_api`, keyed `api:<start path>`. The next crawl of that start URL calls the API directly; if that stops
working, it renders and relearns. With a stored or freshly learned API, the page's links are not walked: the API is
the full list.

Per site, **crawl hints** remember which link patterns yielded items, rank them first, and skip patterns that never do.

## AI

Providers are `groq`, `anthropic` or `openai_compat`, called over plain HTTPS with retries and pacing. Resolution order: the
tenant's own (BYO) default provider, then the platform-shared default. Each call writes an `ai_usage` row (tokens, USD,
purpose, byo). Shared spend is capped by the tenant's `monthly_ai_budget_usd`; BYO spend is not. Keys are Fernet-encrypted
at rest. When AI is unavailable, crawls keep their structured and recipe results and report the problem in `result.ai.errors`.

## Constraints and why

| Constraint | Consequence |
|---|---|
| Cloud Run: CPU only while a request is in flight | jobs run inside a request: `wait=true`, the legacy endpoint, or Cloud Tasks → `/internal/jobs/{id}/run` (Tasks' 30-min deadline < the 60-min request timeout; a retry finds the job already claimed) |
| ephemeral filesystem | connector state (BBMP cookies) and uploads live in the DB; uploads are deleted after the job |
| ≤ 3 DB connections on the shared instance | `max-instances=1`, pool 2+1, every DB use is a short session, never held across network I/O; Postgres `CONNECTION LIMIT 3` |
| multi-tenant | every query is filtered by tenant; recipe sharing only for built-in entities; portal passwords encrypted in job input and shown as `***` |
| SSRF | user URLs and every redirect hop must resolve to public IPs; `X-Backend-Url` must be on an allowlist |
