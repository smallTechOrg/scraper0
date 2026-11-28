"use client";

import { DashboardShell, type NavGroup } from "@/components/shell";
import { SessionGate, TenantScope } from "@/components/session";

const groups: NavGroup[] = [
  {
    items: [
      { href: "/app", label: "Overview", exact: true },
      { href: "/app/sites", label: "Sites" },
      { href: "/app/jobs", label: "Jobs" },
      { href: "/app/sources", label: "Sources" },
      { href: "/app/items", label: "Items" },
    ],
  },
  {
    label: "Configure",
    items: [
      { href: "/app/entities", label: "Entities" },
      { href: "/app/ai", label: "AI" },
    ],
  },
  {
    label: "Workspace",
    items: [
      { href: "/app/api-keys", label: "API keys" },
      { href: "/app/members", label: "Members" },
    ],
  },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionGate>
      <DashboardShell groups={groups}>
        <TenantScope>{children}</TenantScope>
      </DashboardShell>
    </SessionGate>
  );
}
