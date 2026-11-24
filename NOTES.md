# NOTES — scraper0 v0.1 build journal

Branch `feature/scraper0-v0.1`, started 2026-11-24. This is a rewrite in place of `smallTechOrg/web-scraper`; the old Flask
code remains on `main`.

## How it was built
- Intake first ([spec/intake.md](spec/intake.md)), then the contracts ([spec/api.md](spec/api.md) and
  `src/scraper0/connectors/base.py`), then three parallel tracks: core/API/jobs (main), the connector port (agent),
  and the Next.js frontend (agent).
- The engine and learning code (`core/`, `events/`) came from the pre-rewrite `event_engine/` and `scraper_core/`, with
  imports rewritten. The store was rewritten onto the shared SQLAlchemy engine.

## Decisions and deviations
- **Recipes for built-in entities are shared across tenants**: public layouts are learned once for everyone. Custom
  entities are private (`entity_key = tenant_id:name`).
- **Cloud Tasks deadline is 30 min max** (< the 60-min Cloud Run timeout). The request keeps running after the deadline, and
  the retry finds the job already claimed (atomic `UPDATE … WHERE status='queued'`). So background jobs get ~60 min.
- **`/api/v1/scrape` runs the connector as a job inline**, so legacy calls appear in the dashboard and their events land in
  stored results. The error shape is a 422 JSON body. (The old Flask app actually returned a 500 HTML page for most
  handler errors, because ValidationError raised inside the view was unhandled. The backend treats any non-2xx as a failure.)
- **SMARTONEBLR `tracking_id`** is now an int. Legacy returned a string. The backend DTO is `Long`, and Jackson accepts both.
- **TeamEverest fixes** found while porting: the site moved the location key into `data-inline-binding-encoded`, so legacy
  returned `address: null`. Legacy dedupe compared `https` against `http` og:url and never matched.
- **Budget / no-provider errors are surfaced**: the event connectors re-raise them (the job fails with a clear message),
  and the generic crawler keeps structured results and reports `result.ai.errors`.
- **Page-cache fingerprint** = visible text + JSON-LD + meta + image URLs. Visible text alone hashed to the empty string
  for JSON-LD-only pages, so data edits would have been missed.
- **Items on a listing page sharing one link** get `link#hash(name,start)` keys, so they no longer overwrite each other.
- **Captcha**: vision LLM by default, so there's no PyTorch in the image. EasyOCR is still available via `uv sync --extra ocr` +
  `SCRAPER0_CAPTCHA_SOLVER=easyocr|auto`.

## Verified
- Offline suite: `uv run pytest`, 131 passed (API end-to-end, learning, engine, ingest, 70 connector tests), on SQLite
  **and on Postgres 14** with the `scraper0` user at `CONNECTION LIMIT 3` (`SCRAPER0_TEST_POSTGRES_URL=…`). `ruff` is clean.
  Migrations are idempotent on Postgres, and `alembic check` shows no drift.
- Frontend: `tsc`, `eslint` and `next build` are clean. In a real browser against the API on Postgres: login, dashboard,
  New job → TEAMEVEREST connector → live job page with 2 real events stored, and the admin tenants page. No console errors.
- Live: the server boots, migrates and bootstraps. The legacy `POST /api/v1/scrape` TEAMEVEREST call returned 2 real events
  (with addresses), appeared as a job in the dashboard API, and filled the change feed. Connector network tests:
  TEAMEVEREST 2, MYBHARATGOVIN 442, IVOLUNTEERIN 21 events.

## Not verified yet
- **Real AI calls** (no key on the build machine): providers, vision, captcha and recipe learning from a real model.
  Run `uv run pytest -m llm` once a key is in `.env`. Recipe induction was validated on live TeamEverest pages, with the
  legacy parser standing in for the AI.
- **BBMP report/track live**: needs portal credentials. Playwright is mocked in tests.
- **Docker image builds and the Cloud Run deploy**: Docker isn't installed on the build machine, and nothing has been
  deployed. Run `deploy/deploy.sh`.

## Next
- List-page recipes (many items per page from repeated cards): the biggest remaining saving on fetches.
- JS-rendered sites: a Playwright fetch mode for the generic crawler.
- hashtaglocal-backend: send `X-API-Key`, then flip `SCRAPER0_LEGACY_KEYLESS=false`. Optionally consume `/v1/items/changes`.
