# scraper0 architecture

```
                 ┌──────────────── Cloud Run: scraper0-web (Next.js) ───────────────┐
 browser ───────▶│ landing · /login · /app (tenant dashboard) · /admin (platform)     │── rewrites /v1, /v2, /api ─┐
                 └────────────────────────────────────────────────────────────────────┘                            │
 programs (e.g. hashtaglocal-backend) ── X-API-Key ── /v2/sites/… ──┐                                               ▼
 Cloud Scheduler ────── /internal/tick (1/min) ─────────────────────┤      ┌──── Cloud Run: scraper0-api (FastAPI) ────────┐
 Cloud Tasks ────────── /internal/jobs/{id}/run ────────────────────┴─────▶│ api/      routes, auth (key | session), CSRF   │
                                                                            │ jobs/     submit → dispatch → run (atomic claim)│
                                                                            │ services/ sites, site_runs, site_calls, items,  │
                                                                            │           ai (providers, budget, metering)      │
                                                                            │ core/     plans · plan_learn · crawler · fetch ·│
                                                                            │           render · json_extract · llm           │
                                                                            │ events/   event engine, extractors, dedupe      │
                                                                            │ connectors/ TEAMEVEREST MYBHARATGOVIN           │
                                                                            │            IVOLUNTEERIN SMARTONEBLR (Playwright)│
                                                                            └──────────────┬─────────────────────────────────┘
                                                                                           │ ≤ 3 connections
                                                                       Cloud SQL (shared instance) · database `scraper0_<stage>`
```

## Concepts

- **Tenant:** an organisation using scraper0. Users join through invites, with role `admin` or `member`. Programs
  authenticate with tenant API keys. Platform admins manage tenants, access requests and the shared AI providers.
- **Site:** a website, identified by its base URL. Sites are shared by every tenant.
- **Action:** something scraper0 can do on a site, either a read (`fetch_events`, `track_issue`) or a write
  (`report_issue`). Its implementation is either *learned* (plans) or *code* (a connector).
- **Version:** each learn or repair creates a draft version. An admin locks one, and only the locked version serves
  callers. A learned version holds its plans (`implementation_snapshot.plans`) plus the preview its cold run produced.
- **Plan:** the recipe, held as data. See [recipes.md](recipes.md).
- **Item:** a stored result per (site, action, item_key), shared by every tenant, with first seen, last seen and last
  changed times. Each change appends to `item_changes`, whose id is the change-feed cursor.
- **Job:** every learn, refresh and write is a job (`site_learn`, `site_refresh`, `site_write`; a `connector` job
  runs a lookup that outlives its request). Jobs have persisted progress events (served as SSE), a result,
  cancellation and a heartbeat. A **source** is a schedule that refreshes a locked list action.
- **Entity spec:** *what* to extract, described as data (fields, types, required flags). `event` is built in, and
  learned actions currently use it.

## Learning versus running

Learning uses AI where it must. Running a locked plan doesn't; see [recipes.md](recipes.md) for the rule.

**Learning one seed URL:**

1. **Fetch the page.** If it is a JavaScript shell, render it in headless Chromium (`core/render.py`). That uses one
   browser on a dedicated thread, every sub-request passes the SSRF guard, and images, fonts and media are blocked.
   While the page renders, every fetch or XHR call is captured.
2. **If a captured call returns the entity,** `core/plan_learn.py` turns it into a plan. It confirms the mapping,
   paging, tokens and date filters by replaying the call without the browser.
3. **Otherwise the crawler walks the site** (`core/crawler.py`). Each page goes through the cheapest route first:
   structured data (JSON-LD, microdata, ICS, RSS) → the layout rules already learned in this run → heuristics → AI.
   An AI answer is turned into CSS rules for that page layout (`core/recipes.py`), verified by re-extracting the same
   page, and reused for every other page with that layout. The detail pages that produced items define the plan's
   link patterns.
4. **Run each plan cold,** with no browser and no AI. Its output is the version's preview.

**Running:** a refresh runs the locked version's plans (`services/site_runs.py`), or its code connector. The
results are upserted into `items`. Failures keep the stored items and count toward broken.

## AI

- **Providers:** `groq`, `anthropic` or `openai_compat`, called over plain HTTPS with retries and pacing. The tenant's
  own (BYO) default provider is used first, then the platform-shared default.
- **Accounting:** each call writes an `ai_usage` row (tokens, USD, purpose, BYO or shared). Shared spend is capped by
  the tenant's `monthly_ai_budget_usd`; BYO spend is not.
- **Keys** are Fernet-encrypted at rest.
- **AI during reads:** reads use AI only to apply a tenant's event filter and categories. Those verdicts are cached
  per (tenant, item content, options).

## Constraints and why

| Constraint | Consequence |
|---|---|
| Cloud Run: CPU only while a request is in flight | Jobs run inside a request: a read waiting up to `timeout_s`, or Cloud Tasks → `/internal/jobs/{id}/run` (Tasks' 30-min deadline < the 60-min request timeout; a retry finds the job already claimed). |
| Ephemeral filesystem | Connector state (BBMP cookies) lives in the DB. |
| ≤ 3 DB connections on the shared instance | `max-instances=1`, pool 2+1, every DB use is a short session, never held across network I/O; Postgres `CONNECTION LIMIT 3`. |
| Multi-tenant | Sites, actions and items are shared. Write records, AI usage, verdicts, API keys and jobs are per tenant. Portal passwords are encrypted in job input and shown as `***`. |
| SSRF | Every URL and every redirect hop must resolve to public IPs. Plans can only make GET or POST requests through the fetcher. |
