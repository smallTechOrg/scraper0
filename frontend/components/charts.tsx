"use client";

import { useState } from "react";
import type { UsageDay } from "@/lib/api";
import { usd } from "@/lib/format";

/** Fill missing days so the x-axis is continuous. */
function fillDays(days: UsageDay[], count: number): UsageDay[] {
  const map = new Map(days.map((d) => [d.day.slice(0, 10), d]));
  const out: UsageDay[] = [];
  const end = new Date();
  end.setUTCHours(0, 0, 0, 0);
  for (let i = count - 1; i >= 0; i--) {
    const d = new Date(end);
    d.setUTCDate(end.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    out.push(map.get(key) ?? { day: key, usd: 0, calls: 0 });
  }
  return out;
}

function shortDay(iso: string) {
  const d = new Date(iso + "T00:00:00Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

/** Daily spend bars (inline SVG), with an accessible table fallback. */
export function DailySpendChart({ days, range = 30 }: { days: UsageDay[]; range?: number }) {
  const data = fillDays(days, range);
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...data.map((d) => d.usd), 0.0001);
  const W = 600;
  const H = 160;
  const pad = { l: 44, r: 8, t: 10, b: 22 };
  const bw = (W - pad.l - pad.r) / data.length;
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  const ticks = [0, max / 2, max];
  const active = hover !== null ? data[hover] : null;

  return (
    <figure>
      <div className="mb-2 h-5 text-xs text-muted" aria-hidden>
        {active ? (
          <>
            <span className="font-medium text-fg">{shortDay(active.day)}</span> · {usd(active.usd)} · {active.calls} calls
          </>
        ) : (
          "Hover a bar for details"
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-44 w-full" role="img" aria-label={`Daily AI spend, last ${range} days`}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeDasharray={i === 0 ? undefined : "3 3"} />
            <text x={pad.l - 6} y={y(t) + 3} textAnchor="end" fontSize="10" fill="var(--subtle)">
              {usd(t, t < 1 ? 2 : 0)}
            </text>
          </g>
        ))}
        {data.map((d, i) => {
          const h = Math.max(d.usd > 0 ? 1.5 : 0, H - pad.b - y(d.usd));
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={pad.l + i * bw} y={pad.t} width={bw} height={H - pad.t - pad.b} fill="transparent" />
              <rect
                x={pad.l + i * bw + bw * 0.15}
                y={H - pad.b - h}
                width={bw * 0.7}
                height={h}
                rx="2"
                fill="var(--accent)"
                opacity={hover === null || hover === i ? 1 : 0.45}
              />
            </g>
          );
        })}
        {[0, Math.floor(data.length / 2), data.length - 1].map((i) => (
          <text
            key={i}
            x={pad.l + i * bw + bw / 2}
            y={H - 6}
            textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}
            fontSize="10"
            fill="var(--subtle)"
          >
            {shortDay(data[i].day)}
          </text>
        ))}
      </svg>
      <table className="sr-only">
        <caption>Daily AI spend</caption>
        <thead>
          <tr>
            <th>Day</th>
            <th>Spend</th>
            <th>Calls</th>
          </tr>
        </thead>
        <tbody>
          {data
            .filter((d) => d.usd > 0 || d.calls > 0)
            .map((d) => (
              <tr key={d.day}>
                <td>{d.day}</td>
                <td>{usd(d.usd)}</td>
                <td>{d.calls}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </figure>
  );
}

/** Horizontal bars for a breakdown (by purpose, by tenant). */
export function BreakdownBars({
  rows,
  empty = "No usage yet",
}: {
  rows: Array<{ label: string; usd: number; calls: number }>;
  empty?: string;
}) {
  if (rows.length === 0) return <p className="py-4 text-center text-sm text-subtle">{empty}</p>;
  const sorted = [...rows].sort((a, b) => b.usd - a.usd);
  const max = Math.max(...sorted.map((r) => r.usd), 0.0001);
  return (
    <ul className="space-y-3">
      {sorted.map((r) => (
        <li key={r.label}>
          <div className="mb-1 flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate">{r.label}</span>
            <span className="shrink-0 tabular-nums text-muted">
              {usd(r.usd)} <span className="text-xs text-subtle">· {r.calls} calls</span>
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-2" aria-hidden>
            <div className="h-full rounded-full bg-accent" style={{ width: `${(r.usd / max) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}
