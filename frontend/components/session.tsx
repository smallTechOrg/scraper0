"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  ApiError,
  getStoredTenant,
  setStoredTenant,
  type Me,
  type TenantMembership,
  type User,
} from "@/lib/api";
import { Button, ErrorState, Spinner } from "./ui";

interface Session {
  user: User;
  tenants: TenantMembership[];
  tenant: TenantMembership | null;
  /** Tenant admin (or platform admin acting in a tenant). */
  canAdmin: boolean;
  isPlatformAdmin: boolean;
  switchTenant: (id: string) => void;
  logout: () => Promise<void>;
  refresh: () => void;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionGate>");
  return ctx;
}

function pickTenant(me: Me): TenantMembership | null {
  const stored = getStoredTenant();
  return me.tenants.find((t) => t.id === stored) ?? me.tenants[0] ?? null;
}

/**
 * Loads GET /v1/auth/me. 401 → /login?next=…; `requirePlatformAdmin` shows a
 * 403 screen for everyone else.
 */
export function SessionGate({
  children,
  requirePlatformAdmin = false,
}: {
  children: ReactNode;
  requirePlatformAdmin?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    api.me(ctrl.signal).then(
      async (m) => {
        // Platform admins may act in any tenant (X-Tenant), so offer them all.
        if (m.user.is_platform_admin) {
          try {
            const all = await api.admin.tenants();
            const mine = new Set(m.tenants.map((t) => t.id));
            m = {
              ...m,
              tenants: [
                ...m.tenants,
                ...all
                  .filter((t) => !mine.has(t.id))
                  .map((t) => ({ id: t.id, slug: t.slug, name: t.name, role: "admin" as const })),
              ],
            };
          } catch {
            // fall back to memberships only
          }
        }
        if (ctrl.signal.aborted) return;
        setMe(m);
        const t = pickTenant(m);
        setStoredTenant(t?.id ?? null);
        setTenantId(t?.id ?? null);
      },
      (err: unknown) => {
        if (ctrl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace(`/login?next=${encodeURIComponent(pathname)}`);
          return;
        }
        setError(err);
      },
    );
    return () => ctrl.abort();
    // pathname deliberately excluded: only re-check on mount / refresh
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, router]);

  const switchTenant = useCallback((id: string) => {
    setStoredTenant(id);
    setTenantId(id);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setStoredTenant(null);
      router.replace("/login");
    }
  }, [router]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  const session = useMemo<Session | null>(() => {
    if (!me) return null;
    const tenant = me.tenants.find((t) => t.id === tenantId) ?? null;
    return {
      user: me.user,
      tenants: me.tenants,
      tenant,
      canAdmin: tenant?.role === "admin" || me.user.is_platform_admin,
      isPlatformAdmin: me.user.is_platform_admin,
      switchTenant,
      logout,
      refresh,
    };
  }, [me, tenantId, switchTenant, logout, refresh]);

  if (error) {
    return (
      <div className="mx-auto max-w-md py-24">
        <ErrorState error={error} onRetry={refresh} />
      </div>
    );
  }
  if (!session) {
    return (
      <div role="status" className="flex min-h-dvh items-center justify-center gap-2 text-sm text-muted">
        <Spinner className="size-4" /> Loading your workspace…
      </div>
    );
  }
  if (requirePlatformAdmin && !session.isPlatformAdmin) {
    return (
      <div className="mx-auto max-w-md px-4 py-24 text-center">
        <h1 className="text-lg font-semibold">Platform admins only</h1>
        <p className="mt-2 text-sm text-muted">Your account doesn&apos;t have access to the platform admin area.</p>
        <Button className="mt-6" onClick={() => router.push("/app")}>
          Go to dashboard
        </Button>
      </div>
    );
  }

  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

/** Wrap content that needs a selected tenant; remounts on tenant switch. */
export function TenantScope({ children }: { children: ReactNode }) {
  const { tenant, isPlatformAdmin } = useSession();
  if (!tenant) {
    return (
      <div className="mx-auto max-w-md px-4 py-20 text-center">
        <h1 className="text-lg font-semibold">No workspace yet</h1>
        <p className="mt-2 text-sm text-muted">
          You aren&apos;t a member of any tenant. Ask an admin to invite you
          {isPlatformAdmin ? ", or create a tenant in the platform admin area." : "."}
        </p>
        {isPlatformAdmin && (
          <Link href="/admin" className="mt-4 inline-block text-sm font-medium text-accent hover:underline">
            Open platform admin
          </Link>
        )}
      </div>
    );
  }
  return <div key={tenant.id}>{children}</div>;
}

/** Render children only for tenant admins. */
export function AdminOnly({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) {
  const { canAdmin } = useSession();
  return <>{canAdmin ? children : fallback}</>;
}
