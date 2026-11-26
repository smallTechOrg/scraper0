import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "./shell";

export function AuthCard({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center px-4 py-12">
      <Link href="/" className="mb-8" aria-label="scraper0 home">
        <Logo className="text-lg" />
      </Link>
      <div className="w-full max-w-sm rounded-xl border border-border bg-surface p-6 shadow-sm">
        <h1 className="text-lg font-semibold">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
        <div className="mt-6">{children}</div>
      </div>
    </div>
  );
}

/** Only allow same-site relative redirects. */
export function safeNext(next: string | null | undefined, fallback = "/app"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return fallback;
  return next;
}
