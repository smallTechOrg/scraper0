"use client";

export const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;

export function slugify(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^\w\s-]/g, "")
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

/** Parse an optional budget field; returns undefined for blank, Error for invalid. */
export function parseBudget(s: string): number | undefined | Error {
  if (!s.trim()) return undefined;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 ? n : new Error("Budget must be a non-negative number of US dollars.");
}
