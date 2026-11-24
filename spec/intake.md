# scraper0 — intake (2026-11-24)

The decisions behind the v0.1 build. Things marked **Default** were not asked about; correct them here before the build starts.

## What scraper0 is

A standalone, multi-tenant scraping service that replaces `web-scraper`. It extracts structured entities (events first, and any entity type defined as data) from websites, uploads (images, PDFs, CSV/JSON/ICS, pasted text) and site-specific connectors. It learns each page layout once, stores it, and reuses it without the LLM. hashtaglocal is the first tenant. A public landing page lets other people request access.

## Decisions (from the user)

| Topic | Decision |
|---|---|
| Name | **scraper0** (rewrite of the `smallTechOrg/web-scraper` repo in place (branch `feature/scraper0-v0.1`), Python package `scraper0`) |
| Hosting | **Cloud Run.** Request timeout up to 60 min. Live progress for long work is streamed to the client. |
| Tenancy | **Multi-tenant.** Invite-only to start: the landing page has a "request access" form, and the platform admin creates tenants. Self-serve signup can be added later without a redesign. |
| AI provider | Configured by the **platform admin** in the dashboard and **shared across tenants**. Keys are stored encrypted. Each tenant has a monthly AI-spend cap and may add **its own key (BYO)**, which overrides the shared one. |
| Database | **The existing hashtaglocal Postgres instance**, in a **separate `scraper0` database** with its own user. **2–3 connections maximum.** SQLite for local dev and tests. |
| Frontend | **Next.js** (server-rendered) in **`frontend/`** of this repo, deployed separately from the Python API: landing page, tenant dashboard, platform-admin dashboard. |
| v0.1 extras | Async jobs API · scheduled sources · LLM captcha solving · stored results + change feed |
| Keep | `POST /api/v1/scrape` contract used by hashtaglocal-backend (BBMP REPORT_ISSUE/TRACK_ISSUE, TEAMEVEREST/MYBHARATGOVIN/IVOLUNTEERIN/WEBEVENTS FETCH_EVENTS), `GET /api/health`, the generic learning scraper, and image/PDF/CSV/ICS/text ingest. |

## Defaults I'm assuming (not asked)

1. **Stack:** Python 3.12 + uv, FastAPI, SQLAlchemy 2 + Alembic, pydantic-settings, structlog, curl_cffi, Playwright Chromium (BBMP only), ruff, pytest. Same shape as sales0.
2. **Connection cap:** SQLAlchemy pool of 2 + 1 overflow per instance, and Cloud Run `max-instances=1` for the API. The total then stays at 3 or fewer. Raising instances later means lowering the per-instance pool.
3. **Long work on Cloud Run:** a job runs inside an HTTP request so Cloud Run keeps CPU allocated. Either the caller holds a streaming request (Server-Sent Events with progress, then the result), or Cloud Tasks calls `/internal/jobs/{id}/run` with a 60-min deadline. Locally an in-process worker runs jobs. Every job is polled with `GET /v1/jobs/{id}`, or streamed live with `GET /v1/jobs/{id}/events` (SSE).
4. **Scheduler:** Cloud Scheduler calls `POST /internal/tick` every minute with an OIDC/shared secret. The tick enqueues due sources. No always-on threads in production.
5. **Auth:**
   - API: per-tenant API keys, stored hashed and sent as `X-API-Key`.
   - Dashboard: email + password (argon2), with roles platform admin / tenant admin / member and HTTP-only session cookies. Next.js talks to the API server-side.
6. **Compat endpoint:** `/api/v1/scrape` works unchanged for hashtaglocal. It accepts the hashtaglocal tenant's API key. During migration, keyless calls can map to that one configured legacy tenant (env flag, off by default for new deployments), so the backend switches with a URL change and adds the key later.
7. **Secrets:** AI keys and portal credentials are encrypted at rest with a Fernet master key from Secret Manager (env).
8. **AI providers:** Groq (today's default, llama-4-scout, vision-capable), Anthropic Claude, and any OpenAI-compatible endpoint. Every call is metered (tokens, USD) per tenant in a usage ledger.
9. **Captcha:** solved with the vision LLM by default. EasyOCR stays available as an optional extra and is not in the default image, so there's no PyTorch.
10. **Stored results:** items are stored per tenant + entity with a content hash, first_seen / last_seen / changed_at. `GET /v1/items/changes?cursor=…` returns only new or changed items.
11. **Landing page:** what scraper0 does, how it works, the entity/upload/learning features, and a request-access form. Generic product copy, with no hashtaglocal branding.
12. **Git:** local repo with work on a feature branch. Nothing is pushed or committed to a remote without your OK.
13. **Out of v0.1:** list-page recipes (many items per page), JS-rendered sites beyond the BBMP flow, billing/payments, self-serve signup.
