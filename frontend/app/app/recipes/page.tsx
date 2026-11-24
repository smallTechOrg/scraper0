"use client";

import { useMemo, useState } from "react";
import { useConfirm } from "@/components/confirm";
import { useSession } from "@/components/session";
import { Badge, Button, Card, DataView, EmptyState, Input, PageHeader, Select, Table, Td, Th } from "@/components/ui";
import { api, type Recipe } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

// API recipe statuses: "active" (in use) | "stale" (failed 3× in a row; relearned on the next AI read)
function statusTone(s: string) {
  if (s === "active") return "success" as const;
  if (s === "stale") return "warning" as const;
  return "neutral" as const;
}

function successRate(r: Recipe): string {
  const total = r.successes + r.failures;
  return total ? `${Math.round((r.successes / total) * 100)}%` : "—";
}

export default function RecipesPage() {
  const { canAdmin, isPlatformAdmin } = useSession();
  const confirm = useConfirm();
  const [domain, setDomain] = useState("");
  const [entity, setEntity] = useState("");
  const entities = useAsync(() => api.entities(), "entities");
  const recipes = useAsync(() => api.recipes({ entity: entity || undefined }), `recipes|${entity}`);

  const builtinEntities = useMemo(
    () => new Set((entities.data ?? []).filter((e) => e.builtin).map((e) => e.name)),
    [entities.data],
  );
  const canDelete = (r: Recipe) => isPlatformAdmin || (canAdmin && entities.data !== undefined && !builtinEntities.has(r.entity));

  async function remove(r: Recipe) {
    const ok = await confirm({
      title: "Forget this layout?",
      message: (
        <>
          <span className="font-mono text-fg">
            {r.domain}
            {r.path_pattern}
          </span>{" "}
          will be learned again with AI the next time a matching page is scraped.
        </>
      ),
      confirmLabel: "Forget layout",
      action: () => api.deleteRecipe(r.id),
    });
    if (ok) recipes.setData((prev) => (prev ?? []).filter((x) => x.id !== r.id));
  }

  return (
    <>
      <PageHeader
        title="Learned layouts"
        description="Recipes scraper0 learned by reading a page once with AI. Matching pages are extracted with them — no AI needed."
      />
      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3 px-4 py-3 sm:px-5">
          <div className="min-w-[12rem] flex-1">
            <label htmlFor="domain-filter" className="mb-1 block text-xs font-medium text-subtle">
              Domain
            </label>
            <Input
              id="domain-filter"
              type="search"
              className="h-9"
              placeholder="Filter by domain"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="recipe-entity" className="mb-1 block text-xs font-medium text-subtle">
              Entity
            </label>
            <Select id="recipe-entity" className="h-9 w-44" value={entity} onChange={(e) => setEntity(e.target.value)}>
              <option value="">All entities</option>
              {(entities.data ?? []).map((e) => (
                <option key={e.name} value={e.name}>
                  {e.name}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </Card>

      <DataView
        state={recipes}
        empty={
          <Card>
            <EmptyState
              title="No learned layouts yet"
              description="Run a crawl with “Learn layouts” enabled. Each new page layout is learned once and reused."
            />
          </Card>
        }
      >
        {(list) => {
          const needle = domain.trim().toLowerCase();
          const filtered = needle ? list.filter((r) => r.domain.toLowerCase().includes(needle)) : list;
          const groups = new Map<string, Recipe[]>();
          for (const r of filtered) groups.set(r.domain, [...(groups.get(r.domain) ?? []), r]);
          const sorted = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
          if (sorted.length === 0) {
            return (
              <Card>
                <EmptyState title="No layouts match this domain" />
              </Card>
            );
          }
          return (
            <div className="space-y-4">
              {sorted.map(([d, rs]) => (
                <Card key={d}>
                  <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
                    <h2 className="truncate font-mono text-sm font-semibold">{d}</h2>
                    <span className="text-xs text-subtle">
                      {rs.length} layout{rs.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <Table>
                    <thead>
                      <tr>
                        <Th>Path pattern</Th>
                        <Th>Entity</Th>
                        <Th>Status</Th>
                        <Th className="text-right">Successes</Th>
                        <Th className="text-right">Failures</Th>
                        <Th>Updated</Th>
                        <Th>
                          <span className="sr-only">Actions</span>
                        </Th>
                      </tr>
                    </thead>
                    <tbody>
                      {rs.map((r) => (
                        <tr key={r.id} className="hover:bg-surface-2/60">
                          <Td className="max-w-[20rem]">
                            <p className="break-all font-mono text-xs">{r.path_pattern}</p>
                            {r.sample_url && (
                              <a
                                href={r.sample_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="mt-0.5 block truncate text-xs text-accent hover:underline"
                              >
                                sample page
                              </a>
                            )}
                          </Td>
                          <Td>
                            <Badge>{r.entity}</Badge>
                          </Td>
                          <Td>
                            <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                          </Td>
                          <Td className="text-right tabular-nums">{r.successes}</Td>
                          <Td className="text-right tabular-nums">
                            {r.failures}
                            <span className="block text-xs text-subtle">{successRate(r)} ok</span>
                          </Td>
                          <Td className="whitespace-nowrap text-muted">{timeAgo(r.updated_at)}</Td>
                          <Td className="text-right">
                            {canDelete(r) && (
                              <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => remove(r)}>
                                Forget
                              </Button>
                            )}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                </Card>
              ))}
            </div>
          );
        }}
      </DataView>
    </>
  );
}
