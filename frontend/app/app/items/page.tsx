"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ItemsTable } from "@/components/items-table";
import {
  Button,
  Card,
  CardHeader,
  CodeBlock,
  DataView,
  EmptyState,
  Input,
  LoadingState,
  PageHeader,
  Select,
} from "@/components/ui";
import { EntityOptions } from "@/components/job-forms";
import { api } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

const PAGE = 50;

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function ChangeFeedPanel({ entity }: { entity: string }) {
  const [origin, setOrigin] = useState("https://your-scraper0-host");
  useEffect(() => setOrigin(window.location.origin), []);
  const e = entity || "event";
  const code = `# First call: start from cursor 0
curl "${origin}/v1/items/changes?entity=${e}&cursor=0&limit=500" \\
  -H "X-API-Key: s0_your_key"

# → {"data": [{"change": "created", "item": {...}}, ...],
#    "next_cursor": 1842, "has_more": false}

# Next time: pass the stored next_cursor to get only new or changed items
curl "${origin}/v1/items/changes?entity=${e}&cursor=1842" \\
  -H "X-API-Key: s0_your_key"`;
  return (
    <Card>
      <CardHeader
        title="Change feed"
        description="Sync only what's new. Store next_cursor after each call and pass it back as cursor."
      />
      <div className="space-y-3 px-4 py-4 text-sm text-muted sm:px-5">
        <ol className="list-decimal space-y-1 pl-5">
          <li>
            Call <code className="font-mono text-fg">/v1/items/changes</code> with <code className="font-mono text-fg">cursor=0</code> to
            get everything.
          </li>
          <li>
            Save <code className="font-mono text-fg">next_cursor</code>. Keep calling while{" "}
            <code className="font-mono text-fg">has_more</code> is true.
          </li>
          <li>Next sync, pass the saved cursor — you&apos;ll only see items created or updated since.</li>
        </ol>
        <CodeBlock code={code} label="Change feed example" />
        <p className="text-xs">Create an API key under API keys. Each change says whether the item was created or updated.</p>
      </div>
    </Card>
  );
}

function ItemsView() {
  const params = useSearchParams();
  const entities = useAsync(() => api.entities(), "entities");
  const [entity, setEntity] = useState(params.get("entity") ?? "");
  const [q, setQ] = useState("");
  const query = useDebounced(q.trim());
  // Offset resets whenever the filter changes.
  const filterKey = `${entity}|${query}`;
  const [page, setPage] = useState({ key: filterKey, offset: 0 });
  const offset = page.key === filterKey ? page.offset : 0;
  const setOffset = (o: number) => setPage({ key: filterKey, offset: o });

  const items = useAsync(
    () => api.items({ entity: entity || undefined, q: query || undefined, limit: PAGE, offset }),
    `${entity}|${query}|${offset}`,
  );
  const spec = entities.data?.find((e) => e.name === entity);
  const total = items.data?.total ?? 0;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE, total);

  return (
    <>
      <PageHeader title="Items" description="Every stored result, de-duplicated per entity. Updated in place when a page changes." />
      <div className="space-y-6">
        <Card>
          <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3 sm:px-5">
            <div>
              <label htmlFor="entity-filter" className="mb-1 block text-xs font-medium text-subtle">
                Entity
              </label>
              <Select id="entity-filter" className="h-9 w-44" value={entity} onChange={(e) => setEntity(e.target.value)}>
                <option value="">All entities</option>
                {entities.data && <EntityOptions entities={entities.data} />}
              </Select>
            </div>
            <div className="min-w-[12rem] flex-1">
              <label htmlFor="item-search" className="mb-1 block text-xs font-medium text-subtle">
                Search
              </label>
              <Input
                id="item-search"
                type="search"
                className="h-9"
                placeholder="Search names, addresses, links…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
          </div>
          <DataView
            state={items}
            isEmpty={(d) => d.items.length === 0}
            empty={
              <EmptyState
                title={query || entity ? "No matching items" : "No items stored yet"}
                description={query || entity ? "Try another search or entity." : "Successful jobs store their results here."}
              />
            }
          >
            {(d) => (
              <>
                <ItemsTable
                  entity={entity || undefined}
                  fieldOrder={spec?.fields.map((f) => f.name)}
                  metaHeader="Changed"
                  rows={d.items.map((it) => ({
                    key: it.id,
                    data: entity ? it.data : { ...it.data, entity: it.entity },
                    meta: (
                      <time dateTime={it.changed_at} title={`First seen ${formatDate(it.first_seen_at)} · last seen ${formatDate(it.last_seen_at)}`}>
                        {timeAgo(it.changed_at)}
                      </time>
                    ),
                  }))}
                />
                <nav aria-label="Pagination" className="flex items-center justify-between gap-3 px-4 py-3 text-sm sm:px-5">
                  <p className="text-muted tabular-nums">
                    {from}–{to} of {total}
                  </p>
                  <div className="flex gap-2">
                    <Button size="sm" disabled={offset === 0 || items.loading} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                      Previous
                    </Button>
                    <Button size="sm" disabled={to >= total || items.loading} onClick={() => setOffset(offset + PAGE)}>
                      Next
                    </Button>
                  </div>
                </nav>
              </>
            )}
          </DataView>
        </Card>
        <ChangeFeedPanel entity={entity} />
      </div>
    </>
  );
}

export default function ItemsPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <ItemsView />
    </Suspense>
  );
}
