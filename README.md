# scraper0

Turn websites, posters and spreadsheets into structured data. scraper0 is a multi-tenant scraping service that
**learns each page layout once**: the first page of a layout is read with AI, the selectors are learned and verified,
and every similar page after that is scraped without AI.

It started as hashtaglocal's `web-scraper`, which this repo replaces. hashtaglocal is the first tenant, and its
`POST /api/v1/scrape` contract still works unchanged.

## What it does

| | |
|---|---|
| **Any entity type** | `event` is built in. Define others (jobs, NGOs, products…) as data: fields + types + required. |
| **Learning crawler** | Page cache → structured data (JSON-LD, microdata, ICS, RSS) → learned recipe → heuristic → AI, and it learns a recipe from the AI answer. Layout changes are detected and relearned. Recipes for built-in entities are shared across tenants. |
| **Uploads** | Posters/screenshots (vision AI), PDFs, CSV/TSV/JSON, ICS, pasted text → reviewable events with warnings. |
| **Connectors** | TEAMEVEREST, MYBHARATGOVIN, IVOLUNTEERIN (events), SMARTONEBLR (BBMP complaints: report + track, Playwright, AI captcha), WEBEVENTS (any site). |
| **Jobs** | Every scrape is a job: async, or `wait=true`; live progress over SSE; cancel; Cloud Tasks on Cloud Run. |
| **Sources** | Saved crawls/connector runs with an interval; Cloud Scheduler ticks them. |
| **Stored results + change feed** | Items upserted per tenant; `GET /v1/items/changes?cursor=` returns only what is new or changed. |
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

The local database is SQLite under `./data`. Jobs run in a local worker pool, and a scheduler thread ticks sources every minute.

```bash
uv run pytest                     # offline suite (no network, no AI)
uv run pytest -m network          # live portal checks
uv run ruff check src tests
uv run scraper0 --help            # migrate | tick | create-admin | create-tenant | create-api-key | gen-secret-key
```

## Using the API

```bash
KEY=s0_...   # dashboard → API keys
# crawl a site for events (waits for the result)
curl -X POST $API/v1/jobs -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"kind":"crawl","input":{"urls":["https://example.org/events"]},"wait":true}'
# upload a poster
curl -X POST $API/v1/jobs/ingest -H "X-API-Key: $KEY" -F files=@poster.jpg -F wait=true
# only new/changed events since last time
curl "$API/v1/items/changes?entity=event&cursor=$CURSOR" -H "X-API-Key: $KEY"
```

Full contract: [spec/api.md](spec/api.md). Design: [spec/architecture.md](spec/architecture.md). Decisions:
[spec/intake.md](spec/intake.md). Deploying to Cloud Run and switching hashtaglocal over: [docs/deploy.md](docs/deploy.md).

## Layout

```
src/scraper0/
  api/          FastAPI routes (public, tenant, jobs, admin, legacy) + auth
  jobs/         runner (claim/run/progress/cancel), execute (per kind), scheduler, cloud_tasks
  services/     ai (providers, budget, metering), auth, items (change feed), entities, connector_state
  core/         crawler, recipes (layout learning), llm (providers over HTTP), llm_extract, fetch (SSRF/robots), store
  events/       event engine, extractors, ingest (uploads), classify, dedupe
  connectors/   site connectors (base.py = the contract)
  db/           SQLAlchemy models + session (capped pool)
migrations/     Alembic
frontend/       Next.js dashboard + landing page
deploy/         Cloud Run deploy script
```
