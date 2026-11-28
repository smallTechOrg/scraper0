"use client";

import Link from "next/link";
import { Fragment, useMemo, useState, type ReactNode } from "react";
import { cellText, formatDate, hostname, isUrl, looksLikeDate } from "@/lib/format";
import { Badge, Table, Td, Th } from "./ui";

export interface ItemRow {
  key: string;
  data: Record<string, unknown>;
  /** Extra trailing cell (e.g. timestamps on stored items). */
  meta?: ReactNode;
  /** Several extra trailing cells, one per `metaHeaders` entry. */
  metaCells?: ReactNode[];
}

const MAX_COLUMNS = 7;

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v : null;
}

function pick(d: Record<string, unknown>, ...keys: string[]): unknown {
  for (const k of keys) if (d[k] !== undefined && d[k] !== null && d[k] !== "") return d[k];
  return undefined;
}

export function isEventLike(rows: ItemRow[], entity?: string): boolean {
  if (entity === "event") return true;
  const sample = rows.slice(0, 10);
  return (
    sample.length > 0 &&
    sample.every((r) => pick(r.data, "name", "title") !== undefined && ("start_time" in r.data || "start" in r.data))
  );
}

function Value({ v }: { v: unknown }) {
  if (isUrl(v)) {
    return (
      <a href={v} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
        {hostname(v)}
      </a>
    );
  }
  if (looksLikeDate(v)) return <span className="whitespace-nowrap tabular-nums">{formatDate(v)}</span>;
  if (typeof v === "boolean") return <span>{v ? "Yes" : "No"}</span>;
  const t = cellText(v);
  return (
    <span className="line-clamp-2 break-words" title={t.length > 80 ? t : undefined}>
      {t}
    </span>
  );
}

function EventCells({ d }: { d: Record<string, unknown> }) {
  const name = str(pick(d, "name", "title")) ?? "Untitled";
  const link = str(pick(d, "link", "url"));
  const image = str(d.image);
  const start = str(pick(d, "start_time", "start"));
  const end = str(pick(d, "end_time", "end"));
  const address = str(pick(d, "address", "location", "venue"));
  const online = d.is_online === true;
  const org = str(pick(d, "organisation", "organization", "organizer"));
  return (
    <>
      <Td className="min-w-[14rem]">
        <div className="flex items-start gap-3">
          {image && isUrl(image) && (
            // eslint-disable-next-line @next/next/no-img-element -- arbitrary remote hosts
            <img src={image} alt="" loading="lazy" className="size-10 shrink-0 rounded object-cover" />
          )}
          <div className="min-w-0">
            {link && isUrl(link) ? (
              <a href={link} target="_blank" rel="noopener noreferrer" className="font-medium hover:text-accent hover:underline">
                {name}
              </a>
            ) : (
              <span className="font-medium">{name}</span>
            )}
            {org && <p className="truncate text-xs text-subtle">{org}</p>}
          </div>
        </div>
      </Td>
      <Td className="whitespace-nowrap tabular-nums">
        {start ? formatDate(start) : <span className="text-subtle">No date</span>}
        {end && <span className="block text-xs text-subtle">until {formatDate(end)}</span>}
      </Td>
      <Td className="min-w-[12rem]">
        {address ? <span className="line-clamp-2">{address}</span> : null}
        {online && <Badge tone="accent">online</Badge>}
        {!address && !online && <span className="text-subtle">—</span>}
      </Td>
      <Td>
        {link && isUrl(link) ? (
          <a href={link} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
            {hostname(link)}
          </a>
        ) : (
          <span className="text-subtle">—</span>
        )}
      </Td>
    </>
  );
}

/**
 * Render extracted items generically, with a dedicated layout for events.
 * Every row can be expanded to show its full JSON.
 */
export function ItemsTable({
  rows,
  entity,
  metaHeader,
  metaHeaders,
  fieldOrder,
}: {
  rows: ItemRow[];
  entity?: string;
  metaHeader?: string;
  /** Headers for `ItemRow.metaCells` (several trailing columns). */
  metaHeaders?: string[];
  /** Preferred column order (e.g. the entity's field names). */
  fieldOrder?: string[];
}) {
  const [open, setOpen] = useState<string | null>(null);
  const event = isEventLike(rows, entity);

  const columns = useMemo(() => {
    if (event) return [];
    const seen = new Map<string, number>();
    for (const r of rows.slice(0, 100)) {
      for (const [k, v] of Object.entries(r.data)) {
        if (v === null || v === undefined || v === "") continue;
        seen.set(k, (seen.get(k) ?? 0) + 1);
      }
    }
    const ordered = [
      ...(fieldOrder ?? []).filter((k) => seen.has(k)),
      ...[...seen.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k),
    ];
    return [...new Set(ordered)].slice(0, MAX_COLUMNS);
  }, [rows, event, fieldOrder]);

  const colCount = (event ? 4 : columns.length) + 1 + (metaHeader ? 1 : 0) + (metaHeaders?.length ?? 0);

  return (
    <Table>
      <thead>
        <tr>
          {event ? (
            <>
              <Th>Event</Th>
              <Th>When</Th>
              <Th>Where</Th>
              <Th>Link</Th>
            </>
          ) : (
            columns.map((c) => <Th key={c}>{c.replace(/_/g, " ")}</Th>)
          )}
          {metaHeader && <Th>{metaHeader}</Th>}
          {metaHeaders?.map((h) => <Th key={h}>{h}</Th>)}
          <Th className="w-0">
            <span className="sr-only">Details</span>
          </Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const expanded = open === r.key;
          return (
            <Fragment key={r.key}>
              <tr className="hover:bg-surface-2/60">
                {event ? (
                  <EventCells d={r.data} />
                ) : (
                  columns.map((c) => (
                    <Td key={c} className="max-w-[20rem]">
                      <Value v={r.data[c]} />
                    </Td>
                  ))
                )}
                {metaHeader && <Td className="whitespace-nowrap text-xs text-muted">{r.meta}</Td>}
                {metaHeaders?.map((h, i) => (
                  <Td key={h} className="whitespace-nowrap text-xs text-muted">
                    {r.metaCells?.[i]}
                  </Td>
                ))}
                <Td>
                  <button
                    type="button"
                    onClick={() => setOpen(expanded ? null : r.key)}
                    aria-expanded={expanded}
                    className="whitespace-nowrap rounded px-1.5 py-0.5 text-xs text-muted hover:bg-surface-2 hover:text-fg"
                  >
                    {expanded ? "Hide" : "JSON"}
                  </button>
                </Td>
              </tr>
              {expanded && (
                <tr>
                  <td colSpan={colCount} className="border-b border-border bg-surface-2 px-4 py-3">
                    <pre className="max-h-80 overflow-auto font-mono text-xs leading-relaxed">
                      {JSON.stringify(r.data, null, 2)}
                    </pre>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </Table>
  );
}

function When({ at, label }: { at: string | null | undefined; label: string }) {
  if (!at) return <span>—</span>;
  return (
    <time dateTime={at} title={`${label} ${formatDate(at)}`}>
      {timeAgoShort(at)}
    </time>
  );
}

function timeAgoShort(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return formatDate(iso);
}

export interface StoredItemLike {
  id: string;
  entity: string;
  data: Record<string, unknown>;
  site_id: string | null;
  action: string | null;
  first_seen_at: string;
  last_seen_at: string;
  updated_at: string;
}

/**
 * Stored items with their timestamps: first seen, last seen (any run that saw it) and last changed
 * (its content changed). `showSite` adds a Site column (the Items page; the Sites page already knows it).
 */
export function StoredItemsTable({
  items,
  entity,
  fieldOrder,
  showSite = true,
}: {
  items: StoredItemLike[];
  entity?: string;
  fieldOrder?: string[];
  showSite?: boolean;
}) {
  const headers = [...(showSite ? ["Site"] : []), "Last seen", "Last changed", "First seen"];
  return (
    <ItemsTable
      entity={entity}
      fieldOrder={fieldOrder}
      metaHeaders={headers}
      rows={items.map((it) => ({
        key: it.id,
        data: entity ? it.data : { ...it.data, entity: it.entity },
        metaCells: [
          ...(showSite
            ? [
                it.site_id ? (
                  <Link key="site" href={`/app/sites/${encodeURIComponent(it.site_id)}`} title={it.action ?? undefined}
                        className="hover:text-accent hover:underline">
                    {it.site_id}
                    {it.action && <span className="text-subtle"> · {it.action}</span>}
                  </Link>
                ) : (
                  <Badge key="site">—</Badge>
                ),
              ]
            : []),
          <When key="seen" at={it.last_seen_at} label="Last seen" />,
          <When key="changed" at={it.updated_at} label="Content last changed" />,
          <When key="first" at={it.first_seen_at} label="First seen" />,
        ],
      }))}
    />
  );
}
