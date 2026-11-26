"use client";

import { DashboardShell, type NavGroup } from "@/components/shell";
import { SessionGate } from "@/components/session";
import { Badge } from "@/components/ui";

const groups: NavGroup[] = [
  {
    label: "Platform",
    items: [
      { href: "/admin", label: "Tenants", exact: true },
      { href: "/admin/access-requests", label: "Access requests" },
      { href: "/admin/providers", label: "Shared AI providers" },
      { href: "/admin/usage", label: "Usage" },
    ],
  },
  {
    items: [{ href: "/app", label: "← Tenant dashboard" }],
  },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionGate requirePlatformAdmin>
      <DashboardShell groups={groups} badge={<Badge tone="accent">admin</Badge>} showTenantSwitcher={false}>
        {children}
      </DashboardShell>
    </SessionGate>
  );
}
