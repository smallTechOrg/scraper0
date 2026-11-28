# scraper0

Turn the websites you rely on into structured data. scraper0 is a multi-tenant scraping service built around
**onboarded sites and locked actions**: an admin onboards a site, scraper0 learns an action on it once (with AI only
where it must) or runs a reviewed code connector, the admin checks the preview and locks it, and every read after that
runs the locked version's **plan**, a recipe stored as data, without AI. hashtaglocal is the first tenant.

## What it does

| | |
|---|---|
| **Any entity type** | `event` is built in. Define others (jobs, NGOs, products…) as data: fields + types + required. |
| **Learn once** | Learning a site reads it with the cheapest route first: structured data (JSON-LD, microdata, ICS, RSS) → heuristics → AI. Each page layout costs at most one AI call, and the result is turned into CSS rules. |
| **JavaScript and search-API sites** | Pages that are empty until scripts run are rendered in headless Chromium, and every call the page makes (GET or POST, any host) is captured. The one holding the entity is mapped, replayed without the browser, and its paging, tokens and date filters are confirmed by replay. |
| **Plans** | What was learned is stored as a plan ([spec/recipes.md](spec/recipes.md)): request, paging, detail pages, lookups, retries, expectations, fallbacks. A locked plan runs with no browser and no AI. AI can run only in steps that declare it, and is counted per step. |
| **Sites and actions** | Onboard a site (shared by every tenant), learn or attach an action, lock a version: that is the contract callers use (`POST /v2/sites/{site}/actions/{action}/read`). Three failed refreshes mark an action broken; repair learns a new draft to lock. |
| **Code connectors** | TEAMEVEREST, MYBHARATGOVIN, IVOLUNTEERIN (events), SMARTONEBLR (BBMP complaints: report + track, Playwright, AI captcha), seeded as locked code actions. |
| **Jobs** | History of every learn, refresh and write, with live progress over SSE and cancel; Cloud Tasks on Cloud Run. |
| **Schedules** | `/v1/sources` refresh a locked site action on an interval; admins can also "Refresh now". |
| **Stored results + change feed** | One item store per site + action, shared by every tenant; `GET /v1/items/changes?cursor=` returns only what is new or changed. |
| **Multi-tenant** | Invite-only tenants, API keys, roles, per-tenant AI budget; AI providers (Groq, Claude, any OpenAI-compatible) shared by the platform admin, or bring your own key. |
| **Dashboard** | Next.js app in `frontend/`: landing page with request-access form, tenant dashboard, platform admin. |

## Quickstart (local)

```bash
cp .env.example .env              # set the bootstrap admin email/password
uv sync
uv run playwright install chromium   # only for the SMARTONEBLR connector
uv run scraper0 serve             # http://127.0.0.1:5001 · OpenAPI docs: /api/docs
cd frontend && npm install && npm run dev    # http://localhost:3000 — log in with the bootstrap admin
```

AI is configured in the dashboard, not in env: **Admin → Shared AI providers** (Groq, Claude or any
OpenAI-compatible API; keys are encrypted at rest). A tenant can also add its own key under **AI**.

The local database is SQLite under `./data`. Jobs run in a local worker pool, and a scheduler thread ticks schedules every minute.

```bash
uv run pytest                     # offline suite (no network, no AI)
uv run pytest -m network          # live portal checks
uv run ruff check src tests
uv run scraper0 --help            # migrate | tick | create-admin | create-tenant | create-api-key | gen-secret-key
```

## Using the API

```bash
KEY=s0_...   # dashboard → API keys
# onboard the site and lock an action in the dashboard (Sites), then read it with the contract's curl:
curl -X POST $API/v2/sites/karmana-in/actions/fetch_events/read -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"options":{"include_online":false},"freshness_hours":48,"timeout_s":60}'
# only new/changed events since last time
curl "$API/v1/items/changes?entity=event&cursor=$CURSOR" -H "X-API-Key: $KEY"
```

Full contract: [spec/api-v2.md](spec/api-v2.md) (sites and actions) and [spec/api.md](spec/api.md) (auth, jobs history,
schedules, items). Plans: [spec/recipes.md](spec/recipes.md). Design: [spec/architecture.md](spec/architecture.md) and
[spec/sites-and-actions.md](spec/sites-and-actions.md). Deploying to Cloud Run: [docs/deploy.md](docs/deploy.md).

## Layout

```
src/scraper0/
  api/          FastAPI routes (public, tenant, jobs, admin, v2 sites and actions) + auth
  jobs/         runner (claim/run/progress/cancel), execute (per kind), scheduler, cloud_tasks
  services/     sites, site_runs (learn/refresh/write), site_calls (reads, writes), items (change feed), ai, auth
  core/         plans (schema + runner), plan_learn, crawler (learning only), recipes (layout rules),
                json_extract (JSON mapping), fetch (SSRF/robots), render (headless Chromium), llm, signed_urls
  events/       event engine, extractors, classify, dedupe
  connectors/   site connectors (base.py = the contract)
  db/           SQLAlchemy models + session (capped pool)
migrations/     Alembic (one baseline)
frontend/       Next.js dashboard + landing page
deploy/         Cloud Run deploy script
```
