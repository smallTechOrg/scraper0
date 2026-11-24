"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChangePasswordModal } from "./change-password";
import { useSession } from "./session";
import { cx, Select } from "./ui";

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-2 font-semibold tracking-tight", className)}>
      <svg viewBox="0 0 24 24" className="size-6" aria-hidden>
        <rect x="2" y="2" width="20" height="20" rx="6" fill="var(--accent)" />
        <path d="M7 9h10M7 12h7M7 15h4" stroke="var(--accent-fg)" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
      <span>
        scraper<span className="text-accent">0</span>
      </span>
    </span>
  );
}

export interface NavItem {
  href: string;
  label: string;
  /** Match exactly instead of by prefix. */
  exact?: boolean;
}

export interface NavGroup {
  label?: string;
  items: NavItem[];
}

function NavLinks({ groups, onNavigate }: { groups: NavGroup[]; onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="space-y-5">
      {groups.map((g, i) => (
        <div key={g.label ?? i}>
          {g.label && <p className="mb-1 px-3 text-xs font-medium uppercase tracking-wide text-subtle">{g.label}</p>}
          <ul className="space-y-0.5">
            {g.items.map((item) => {
              const active = item.exact ? pathname === item.href : pathname === item.href || pathname.startsWith(item.href + "/");
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cx(
                      "block rounded-md px-3 py-1.5 text-sm transition-colors",
                      active ? "bg-accent-soft font-medium text-accent-soft-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
                    )}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function TenantSwitcher() {
  const { tenants, tenant, switchTenant } = useSession();
  if (tenants.length === 0) return null;
  if (tenants.length === 1) {
    return (
      <div className="min-w-0 text-sm">
        <span className="block truncate font-medium">{tenant?.name}</span>
        <span className="block text-xs text-subtle">{tenant?.role}</span>
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <label htmlFor="tenant-switcher" className="sr-only">
        Tenant
      </label>
      <Select
        id="tenant-switcher"
        value={tenant?.id ?? ""}
        onChange={(e) => switchTenant(e.target.value)}
        className="h-9 max-w-[14rem]"
      >
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name} ({t.role})
          </option>
        ))}
      </Select>
    </div>
  );
}

function UserMenu() {
  const { user, isPlatformAdmin, logout } = useSession();
  const [open, setOpen] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const pathname = usePathname();
  const inAdmin = pathname.startsWith("/admin");

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initials = (user.name || user.email)
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase())
    .join("");

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex items-center gap-2 rounded-full p-0.5 hover:bg-surface-2"
      >
        <span className="flex size-8 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent-soft-fg">
          {initials}
        </span>
        <span className="sr-only">Account menu for {user.name || user.email}</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-40 mt-2 w-60 rounded-lg border border-border bg-surface p-1 shadow-lg"
        >
          <div className="border-b border-border px-3 py-2">
            <p className="truncate text-sm font-medium">{user.name}</p>
            <p className="truncate text-xs text-subtle">{user.email}</p>
          </div>
          {isPlatformAdmin && (
            <Link
              role="menuitem"
              href={inAdmin ? "/app" : "/admin"}
              onClick={() => setOpen(false)}
              className="block rounded-md px-3 py-2 text-sm hover:bg-surface-2"
            >
              {inAdmin ? "Tenant dashboard" : "Platform admin"}
            </Link>
          )}
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              setOpen(false);
              setChangingPassword(true);
            }}
            className="block w-full rounded-md px-3 py-2 text-left text-sm hover:bg-surface-2"
          >
            Change password
          </button>
          <button
            role="menuitem"
            type="button"
            onClick={() => logout()}
            className="block w-full rounded-md px-3 py-2 text-left text-sm hover:bg-surface-2"
          >
            Log out
          </button>
        </div>
      )}
      {changingPassword && <ChangePasswordModal onClose={() => setChangingPassword(false)} />}
    </div>
  );
}

export function DashboardShell({
  groups,
  children,
  badge,
  showTenantSwitcher = true,
}: {
  groups: NavGroup[];
  children: ReactNode;
  badge?: ReactNode;
  showTenantSwitcher?: boolean;
}) {
  const [drawer, setDrawer] = useState(false);
  const pathname = usePathname();
  useEffect(() => setDrawer(false), [pathname]);

  const brand = (
    <Link href={groups[0]?.items[0]?.href ?? "/"} className="flex items-center gap-2">
      <Logo />
      {badge}
    </Link>
  );

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_1fr]">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-dvh flex-col border-r border-border bg-surface lg:flex">
        <div className="flex h-14 items-center px-4">{brand}</div>
        <div className="flex-1 overflow-y-auto px-2 py-3">
          <NavLinks groups={groups} />
        </div>
      </aside>

      {/* Mobile drawer */}
      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40" aria-hidden onClick={() => setDrawer(false)} />
          <div
            id="mobile-nav"
            className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col border-r border-border bg-surface shadow-xl"
          >
            <div className="flex h-14 items-center justify-between px-4">
              {brand}
              <button
                type="button"
                onClick={() => setDrawer(false)}
                className="rounded p-1 text-subtle hover:bg-surface-2"
                aria-label="Close menu"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-2 py-3">
              <NavLinks groups={groups} onNavigate={() => setDrawer(false)} />
            </div>
          </div>
        </div>
      )}

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-surface/90 px-4 backdrop-blur sm:px-6">
          <button
            type="button"
            className="-ml-1 rounded p-1.5 text-muted hover:bg-surface-2 lg:hidden"
            onClick={() => setDrawer(true)}
            aria-label="Open menu"
            aria-expanded={drawer}
            aria-controls="mobile-nav"
          >
            <svg viewBox="0 0 20 20" className="size-5" fill="currentColor" aria-hidden>
              <path d="M3 5h14v1.5H3zM3 9.25h14v1.5H3zM3 13.5h14V15H3z" />
            </svg>
          </button>
          <div className="lg:hidden">{brand}</div>
          <div className="ml-auto flex min-w-0 items-center gap-3">
            {showTenantSwitcher && <TenantSwitcher />}
            <UserMenu />
          </div>
        </header>
        <main id="main" className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
          {children}
        </main>
      </div>
    </div>
  );
}
