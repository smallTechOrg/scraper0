# Recipes: plans

A **plan** is how scraper0 gets a site's items, stored as data in the action's version
(`action_versions.implementation_snapshot.plans`). Learning a site produces plans; refreshing a locked version only
runs them. Code: `src/scraper0/core/plans.py` (schema and runner) and `core/plan_learn.py` (learning from what a page
fetched).

## Minimum AI

- **AI only in declared steps.** Only `ai` and `captcha` steps may call AI, and each must say `"uses_ai": true`. No
  other step ever receives an AI client. A plan without such steps runs at zero tokens.
- **Cheapest route first.** Learning tries structured data, then heuristics, then AI. An `ai` step only fills fields
  that the non-AI steps left empty, and only for the items that need them. A `captcha` step uses the non-AI solver
  before the AI.
- **Every call is accounted.** Run stats report `ai_calls`, `ai_tokens` and `ai_by_step`, and the job result
  carries them. Calls go through the tenant's AI budget like any other AI use.
- **No browser at run time.** This version has no browser steps, so a locked plan never opens one. Browser steps
  come later.

## Shape

```json
{
  "plan_version": 1,
  "entity": "event",
  "entry": {"direct": "https://site/events", "root": "https://site/",
            "navigate": [{"link": {"text": ["Events", "All events"], "href_contains": ["/events"]}}]},
  "steps": [
    {"id": "list", "type": "request", "method": "POST", "url": "https://api.site/search",
     "headers": {"content-type": "application/json", "origin": "https://site"},
     "body": {"json": {"page": "{page.value}", "limit": 12, "date_gt": "{today}"}},
     "paginate": {"mode": "page", "start": 1, "type": "str", "max_pages": 25,
                  "stop": {"fewer_than": 12, "total_path": "total"}},
     "items": {"json": "records.*._source"},
     "expect": {"min_items": 1},
     "retry": {"attempts": 3, "backoff_s": 1}},
    {"id": "orgs", "type": "lookup", "request": {"url": "https://api.site/orgs"}, "items": {"json": "data"},
     "field": "organisation", "key": "org_id", "id_key": "id", "value": "name"}
  ],
  "output": "list",
  "mapping": {"name": "event_name", "start_time": ["event_from_date", "event_from_time"],
              "image": {"template": "https://cdn.site/events/{image_path}"}},
  "link_template": "https://site/events/{slug}",
  "link_base": "https://site/"
}
```

### Step types

| Type | What it does |
|---|---|
| `request` | One HTTP call (GET or POST; `query`, `headers`, and `body` as `json`, `form` or `text`). With `items` it yields records: a JSON path (`records.*._source` maps over an array), or `links` matching path patterns in HTML. |
| `for_each` | One request per record of an earlier step (`over`), capped by `max`. `extract` reads fields from the HTML: `structured` (JSON-LD and microdata) and `selectors` (CSS rules per path pattern). With `optional: true`, a failed request keeps the record. |
| `lookup` | Fetches a second list and joins it in (`key` on the item → `id_key` on the other list → `value` into `field`). If it fails, only that field stays empty. |
| `any_of` | Ordered `alternatives` (any step type). The first that succeeds wins, and the run records which one. |
| `ai` | **Uses AI.** For records of `over` that are missing `fields`, reads the page with the AI and fills only those fields. |
| `captcha` | **Uses AI.** Non-AI solver first, then the AI. It needs browser steps, so it isn't executable yet. |

Reserved for later versions (rejected as "not supported yet"): `browser`, `session`, `extract`, `commit`, `verify`,
`code`.

### On any step

| Key | Meaning |
|---|---|
| `paginate` | `offset` (`start`, `step`), `page` (`start`), `cursor` (`cursor_path`), or `next_link` (HTML "next" links). It stops when a page is empty, repeats the previous page, has `fewer_than` items, `total_path` has been reached, or `max_pages` (at most 25) is hit. The current value is `{page.value}`; `type: "str"` keeps it a string. |
| `capture` | Takes values from the response for later templates (`{captures.name}`): `json` (path), `regex` (first group), `header`, `cookie`. |
| `retry` | `{attempts ≤ 5, backoff_s ≤ 30}`. It retries **transient failures only**: no response, 5xx, and 429 (honouring `Retry-After`). The default is 2 attempts with a 1 s backoff. |
| `expect` | Post-checks: `min_items`, `status`, and `coverage: {fields, min_ratio}`. A failed expect is a step failure, so breakage that would otherwise be silent is caught. |
| `on_failure` | `fail` (the default; `skip` for lookups), `skip`, or `{"goto": "<later step>"}`. A step marked `fallback_only` runs only when jumped to, and its items stand in for the step that failed. |
| `when` | `{"captured": name, "op": exists\|missing\|equals\|not_equals\|in, "value"}` or `{"step": id, "outcome": ok\|failed\|skipped}`. If false, the step is skipped. |

### Templates

Templates do plain substitution only: `{a.b}` looks up a dotted path in the context, and nothing is evaluated. The
context holds `seed`, `entry`, `today` (Asia/Kolkata), `from` (the caller's option), `input`, `page`, `captures`, and
`item` (inside `for_each`). A string that is exactly one placeholder keeps the value's type.

### Entry and navigation

Steps that list items use `{entry}`, which is `entry.direct`: the deepest stable URL, such as the list page. If the
direct URL fails or yields no items, the runner follows `entry.navigate` from `root`. Each hop picks the first link
whose text or href matches one of its alternatives. The runner then runs again from the URL it reached, and records
`entry: "navigate"` and `entry_changed_to` in the run stats.

### Run stats

Each run reports (job result → `plan_runs`):

- **Volume:** `requests`, `pages`, `items` and `detail_failures`.
- **AI:** `ai_calls`, `ai_tokens` and `ai_by_step`.
- **Path taken:** `entry`, `entry_changed_to`, `alternatives` (the winner per `any_of`), `retries` (per step),
  `expect_failures` and `outcomes` (`ok`, `failed`, `skipped` or `fallback` per step).

A failed run keeps the stored items and counts toward the action's `consecutive_failures`. After three, the action
is `broken`, and a repair learns a new draft that an admin locks.

## Learning

Learning runs once per seed URL, with AI allowed.

1. **Load the page.** Static pages are fetched. JavaScript pages are rendered, recording every fetch or XHR call
   (any method, any host). Credential headers are dropped from the record, and their values stay in memory only
   while learning.
2. **API sites.**
   1. Pick the captured call whose JSON maps best to the entity, using heuristics first and AI only if they fail
      (at most 3 calls). The call becomes a `request` step.
   2. Replay it without the browser. If the replay fails, add a warm-up request for cookies, or a `capture` step
      for a token that an earlier response contained. If neither fixes it, the site is not learned, and the error
      says why.
   3. Confirm paging parameters by replaying: a new value must give different items, and the same value the same
      items. A date in the body equal to today becomes `{today}`, but only if changing it changes the results.
   4. Image values that are not URLs get a prefix template inferred from the page's `<img>` sources.
   5. Lookups (for example organiser names) come from the page's other GET calls.
3. **Server-rendered sites.** The crawler learns each detail-page layout once (usually one AI call), then reuses it
   for the other pages of that layout. The plan becomes: list page (`next_link` paging) → detail links matching the
   learned patterns → structured data and CSS rules. `navigate` records the home-page link that leads to the list
   page.
4. **Verify before keep.** Every plan runs cold, with no browser and no AI. The version's preview is that run's
   output, so a draft shows exactly what a locked refresh will produce.

## Recipes as files (backup and restore)

Everything a locked version needs to run can be written to files and read back, so what scraper0 has learned
survives losing the database. Code: `src/scraper0/services/recipe_export.py`.

```
scraper0 export-recipes <dir> [--prune]     # one file per locked action: <dir>/<site_id>/<action>.json + index.json
scraper0 import-recipes <dir|bundle.json> [--lock]
GET /v2/sites/{site_id}/actions/{name}/export     (tenant admin)     → {"data": Recipe}
GET /v2/export                                    (platform admin)   → {"data": {"format": "scraper0.recipes/1", "recipes": [Recipe]}}
```

```json
{
  "format": "scraper0.recipe/1",
  "site": {"site_id": "karmana-in", "base_url": "https://karmana.in", "name": "karmana.in"},
  "action": {"name": "fetch_events", "kind": "read", "mode": "list", "entity": "event",
             "seed_urls": ["https://karmana.in"], "input_schema": {}, "output_schema": {}},
  "version": 3, "locked_at": "2026-11-28T06:05:42Z", "locked_by": "admin@example.com",
  "implementation": {"implementation": "learned", "entity": "event", "seed_urls": ["https://karmana.in"],
                     "plans": [{"seed": "https://karmana.in", "plan": {"plan_version": 1, "…": "…"}}]}
}
```

A code action's `implementation` is `{"implementation": "code", "connector": "TEAMEVEREST", "action": "FETCH_EVENTS"}`.

- **Diff-friendly.** Keys are sorted, the layout is fixed, and a file is rewritten only when its content changes.
  Exporting twice with nothing changed touches no file. `index.json` lists the files on disk with a `sha256` of
  what each recipe does (site, action, implementation), not of who locked it or when.
- **No run state, no secrets.** Previews, learn statistics, freshness and failure counters are left out.
  Credential headers (`Authorization`, `Cookie`, `X-Api-Key`, …) are stripped on export, and a file that carries
  one is refused on import.
- **Import.** Each file is validated (the plan with the plan schema, a code action against the connectors in this
  build). The site and action are created if missing. If the action's locked version already does the same thing,
  nothing changes. Otherwise the recipe becomes the next version: a draft, or the locked one with `--lock`.
  A bad file is reported and the others are still imported. The command exits non-zero if any file failed.
- **Export on lock.** With `SCRAPER0_RECIPE_EXPORT_DIR` set, locking a version writes its file (and the index)
  there. Point it at a checkout of a git repository and commit from a cron job or CI; scraper0 does not push.
  A failed export is logged and never fails the lock. `--prune` on the command removes files of actions that are
  no longer locked.
