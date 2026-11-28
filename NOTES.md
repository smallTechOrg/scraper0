# NOTES — scraper0

Working notes on how the current system behaves and why. The contracts are in `spec/`.

## Model

- **Onboarding is the only way in.** Sites → actions → versions → lock. Callers use `site_id` + action only.
  Learned actions are reads of the built-in `event` entity. Writes are code connectors (BBMP).
- **One recipe format: plans** ([spec/recipes.md](spec/recipes.md)), frozen into each version. Learning uses a crawler
  with in-memory scratch knowledge for that run only: layout rules, link hints, page cache. Nothing learned outlives
  the run except the plan.
- **Minimum AI.** AI is used to learn and repair, and at run time only in plan steps that declare it (`ai`,
  `captcha`). Each such step is accounted per step. A refresh of a plan without them makes zero AI calls; a test
  asserts this.
- **One item store,** shared by every tenant, keyed (site, action, item_key) → `itm_…` ids. Every refresh bumps
  `last_seen_at`; `updated_at` moves only on content changes. Image links are not content.
- **Images are links only.** Copying images is the caller's job. Signed links carry `image_expires_at`, and a read
  rescrapes first when stored links expire within 10 minutes.

## Behaviour worth knowing

- **The preview is a cold run.** A learn ends by running its plans with no browser and no AI. The preview is that
  output, so a draft shows exactly what refreshes will produce. If that run yields nothing, the version is `broken`.
- **Failures:** a failed refresh (a step error, a failed `expect`, 0 items after a run that had items) keeps the
  stored items and counts toward `consecutive_failures`. After 3 the action is `broken`, and a repair learns a
  new draft for an admin to lock.
- **Server-rendered sites:** the plan's link patterns come from the detail pages that produced items while learning.
  `entry.navigate` records the home-page link to the list page, so a moved list page is found again (the run stats
  then say `entry: navigate`).
- **Search APIs (POST):** the request body is kept as captured, except that confirmed paging parameters become
  `{page.value}` and a date equal to today becomes `{today}` (only when changing it changes the results). Credential
  headers are never stored. A needed token is captured at run time from the response it came from, and if it can't
  be found the site isn't learned.
- **Cloud Tasks deadline is 30 min** (< the 60-min Cloud Run timeout). The request keeps running, and the retry finds
  the job already claimed. Background jobs therefore get about 60 min.
- **Captcha (BBMP connector):** vision AI by default. EasyOCR is available with `uv sync --extra ocr` and
  `SCRAPER0_CAPTCHA_SOLVER=easyocr|auto`.
- **Code connectors** still contain their own optional AI filter (`event_filter`/`category_filter` in connector input).
  v2 reads never pass a filter to them (filtering happens per tenant at read time), so a code action's refresh makes no
  AI calls.

## Schema

There is a single Alembic baseline (`migrations/versions/0001_baseline.py`). A database built from an older history
must be reset before deploying.

## Running the checks

```bash
uv run pytest                     # offline, SQLite
SCRAPER0_TEST_POSTGRES_URL=postgresql+psycopg://scraper0:pw@127.0.0.1:55432/scraper0 uv run pytest   # Postgres, 3-connection limit
uv run pytest -m network          # live portal checks
uv run ruff check src tests
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

`playwright install chromium` may remove older Chromium builds from `~/Library/Caches/ms-playwright`, which other
repos on the same machine may need.


## Learner gaps closed (2026-11-28)

After the first learned-vs-connector comparison, these generic mechanisms were added (`spec/recipes.md` has the
detail; `tests/test_learner_gaps.py` has an offline fake site for each):

- **Values beyond visible text:** rules read attributes, meta tags, encoded values and inline JSON (`decode`).
- **Templates, not URL patterns:** rules are shared across URL patterns of one template; the AI budget is per
  template and pattern; detail pages go to the AI one at a time.
- **Why learning cost 30 AI calls before:** a static site was rendered page by page once one page had been
  rendered, and every rendered page asked the AI to map its analytics calls. Both are fixed.
- **Paging:** stops at the API's total; caps are settings and reported when hit; page size is raised; unstable
  order gets a confirmed tiebreaker or is swept.
- **Links:** the item's own URL key, script URLs, or click-and-see in the browser, verified by a GET.
- **Listing discovery:** a thin seed gives way to the site's listing page.
- **Placeholder images** count as no image.
- **AI answers:** unbalanced JSON is repaired; site chrome (logos, header images) is not offered as a picture.

Not covered: values that exist only after a script computes them (a derived end time), and sites whose listing
has no link that names it.

## Action plans: browser steps, sessions, captcha, writes (2026-11-28)

- `core/action_plans.py` runs lookups and writes from data in a browser: `browser`, `session`, `captcha`, `extract`,
  `expect`, `any_of`, with `commit` and `verify` for writes. List plans (`core/plans.py`) are unchanged; `plans.validate`
  routes `"kind": "action"` plans to the new module.
- `Renderer.session(fn)` runs a whole browser session on the existing browser thread, with the same SSRF guard.
  Sessions are serial with renders: a long write holds the browser while it runs.
- The BBMP connector is translated into two plans (`site_plans/smartoneblr-com/`), seeded as draft actions
  `track_issue_plan` and `report_issue_plan`. The code connectors stay until the plans are locked and proven.
- Checked against the real portal, read-only: the track plan returns the same output as the code connector for a
  real complaint number, and the report plan's login-modal targets all match. **The report plan has never been run
  past the login modal on the real portal** (it files real complaints, and there are no credentials here).
- The captcha solver still lives in `connectors/bbmp/captcha.py`; move it to `core/` when the connector goes.
- Browser tests are marked `browser` (run with `uv run pytest -m browser`); they use `tests/fake_portal.py`.

