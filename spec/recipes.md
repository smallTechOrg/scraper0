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
