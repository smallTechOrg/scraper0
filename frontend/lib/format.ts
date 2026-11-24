export function formatDate(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(
    undefined,
    withTime
      ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "short", day: "numeric" },
  );
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  const abs = Math.abs(s);
  const fmt = (n: number, unit: string) => (s >= 0 ? `${n}${unit} ago` : `in ${n}${unit}`);
  if (abs < 45) return s >= 0 ? "just now" : "in a moment";
  if (abs < 3600) return fmt(Math.round(abs / 60), "m");
  if (abs < 86400) return fmt(Math.round(abs / 3600), "h");
  if (abs < 86400 * 30) return fmt(Math.round(abs / 86400), "d");
  return formatDate(iso, false);
}

export function duration(start: string | null | undefined, end: string | null | undefined): string {
  if (!start) return "—";
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(a) || Number.isNaN(b)) return "—";
  const s = Math.max(0, Math.round((b - a) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function usd(n: number | null | undefined, digits?: number): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const d = digits ?? (Math.abs(n) < 1 && n !== 0 ? 4 : 2);
  return `$${n.toFixed(d)}`;
}

export function hostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** Render any JSON-ish value as a short string for table cells. */
export function cellText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function isUrl(v: unknown): v is string {
  return typeof v === "string" && /^https?:\/\//i.test(v);
}

export function looksLikeDate(v: unknown): v is string {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v);
}

export function jobErrorText(err: unknown): string | null {
  if (!err) return null;
  if (typeof err === "string") return err;
  if (typeof err === "object") {
    const e = err as { message?: unknown; code?: unknown };
    if (typeof e.message === "string") return e.code ? `${e.message} (${String(e.code)})` : e.message;
  }
  return cellText(err);
}
