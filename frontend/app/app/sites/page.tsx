"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AdminOnly } from "@/components/session";
import { ActionChips, OnboardSiteModal, sitePath } from "@/components/sites";
import { Button, Card, DataView, EmptyState, Input, PageHeader, Table, Td, Th } from "@/components/ui";
import { sitesApi } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

export default function SitesPage() {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [onboarding, setOnboarding] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 250);
    return () => clearTimeout(t);
  }, [query]);

  const sites = useAsync(() => sitesApi.list(q || undefined), `sites|${q}`);

  const onboardButton = (
    <AdminOnly>
      <Button variant="primary" onClick={() => setOnboarding(true)}>
        Onboard a site
      </Button>
    </AdminOnly>
  );

  return (
    <>
      <PageHeader
        title="Sites"
        description="Websites scraper0 knows, and the actions it can run on them. Sites are shared across tenants. Callers only use actions with a locked version."
        actions={onboardButton}
      />

      <Card>
        <div className="border-b border-border px-4 py-3 sm:px-5">
          <label htmlFor="site-search" className="sr-only">
            Search sites
          </label>
          <Input
            id="site-search"
            type="search"
            className="h-9 sm:max-w-sm"
            placeholder="Search by name, site id or URL"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <DataView
          state={sites}
          empty={
            q ? (
              <EmptyState title="No matching sites" description={`Nothing matches "${q}". Onboard it to add it.`} action={onboardButton} />
            ) : (
              <EmptyState
                title="No sites yet"
                description="Onboard a site to learn an action on it, such as fetching its events, then lock it for callers."
                action={onboardButton}
              />
            )
          }
        >
          {(list) => (
            <Table>
              <thead>
                <tr>
                  <Th>Site</Th>
                  <Th className="hidden md:table-cell">Base URL</Th>
                  <Th>Actions</Th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.site_id} className="hover:bg-surface-2/60">
                    <Td className="min-w-[12rem]">
                      <Link href={sitePath(s.site_id)} className="font-medium hover:text-accent hover:underline">
                        {s.name || s.site_id}
                      </Link>
                      <p className="font-mono text-xs text-subtle">{s.site_id}</p>
                      <p className="mt-0.5 truncate text-xs text-muted md:hidden">{s.base_url}</p>
                    </Td>
                    <Td className="hidden max-w-[18rem] md:table-cell">
                      <a
                        href={s.base_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="break-all text-muted hover:text-accent hover:underline"
                      >
                        {s.base_url}
                      </a>
                    </Td>
                    <Td>
                      <ActionChips actions={s.actions ?? []} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </DataView>
      </Card>

      <OnboardSiteModal open={onboarding} onClose={() => setOnboarding(false)} />
    </>
  );
}
