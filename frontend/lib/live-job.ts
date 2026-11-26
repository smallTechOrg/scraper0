"use client";

import { useCallback, useEffect, useState } from "react";
import { api, TERMINAL_STATUSES, type Job } from "./api";

export interface LogLine {
  id: number;
  at: string;
  level?: string;
  message: string;
}

export type LiveMode = "connecting" | "live" | "polling" | "done";

const LIVE_SAFETY_POLL_MS = 10_000; // while SSE is connected, as a safety net
const FALLBACK_POLL_MS = 2_000; // when SSE isn't available

function parse(data: unknown): unknown {
  if (typeof data !== "string") return data;
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

let logSeq = 0;

function toLog(raw: unknown): LogLine {
  const d = parse(raw);
  const at = new Date().toISOString();
  if (isObject(d)) {
    const message = typeof d.message === "string" ? d.message : JSON.stringify(d);
    return {
      id: ++logSeq,
      at: typeof d.at === "string" ? d.at : typeof d.ts === "string" ? d.ts : at,
      level: typeof d.level === "string" ? d.level : undefined,
      message,
    };
  }
  return { id: ++logSeq, at, message: String(d) };
}

export const isTerminal = (j: Pick<Job, "status"> | null | undefined) =>
  !!j && TERMINAL_STATUSES.includes(j.status);

/**
 * Follow a job live: EventSource on /v1/jobs/{id}/events (progress / log /
 * done), with polling of GET /v1/jobs/{id} as a fallback and safety net.
 */
export function useLiveJob(id: string) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [mode, setMode] = useState<LiveMode>("connecting");
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let stopped = false;
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let interval = LIVE_SAFETY_POLL_MS;
    const ctrl = new AbortController();

    const stop = () => {
      stopped = true;
      es?.close();
      es = null;
      if (timer) clearTimeout(timer);
    };

    const finish = () => {
      stop();
      setMode("done");
    };

    async function fetchJob(): Promise<Job | null> {
      try {
        const j = await api.job(id, ctrl.signal);
        if (!stopped) {
          setJob(j);
          setError(null);
        }
        return j;
      } catch (err) {
        if (!stopped && !ctrl.signal.aborted) setError(err);
        return null;
      }
    }

    function schedulePoll() {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(async () => {
        const j = await fetchJob();
        if (stopped) return;
        if (isTerminal(j)) finish();
        else schedulePoll();
      }, interval);
    }

    function fallbackToPolling() {
      es?.close();
      es = null;
      interval = FALLBACK_POLL_MS;
      setMode("polling");
      schedulePoll();
    }

    function openStream() {
      if (typeof EventSource === "undefined") return fallbackToPolling();
      es = new EventSource(api.jobEventsUrl(id), { withCredentials: true });
      es.onopen = () => {
        if (!stopped) setMode("live");
      };
      es.addEventListener("progress", (ev) => {
        const d = parse((ev as MessageEvent).data);
        const patch = isObject(d) ? d : { message: String(d) };
        setJob((j) =>
          j
            ? {
                ...j,
                status: j.status === "queued" ? "running" : j.status,
                progress: { ...(j.progress ?? {}), ...patch },
              }
            : j,
        );
      });
      es.addEventListener("log", (ev) => {
        const line = toLog((ev as MessageEvent).data);
        setLogs((l) => [...l.slice(-499), line]);
      });
      es.addEventListener("done", async () => {
        es?.close();
        es = null;
        const j = await fetchJob();
        if (stopped) return;
        if (isTerminal(j)) finish();
        else fallbackToPolling();
      });
      es.onerror = () => {
        if (!stopped) fallbackToPolling();
      };
    }

    (async () => {
      const j = await fetchJob();
      if (stopped) return;
      if (!j) {
        // couldn't load — retry slowly; error is shown meanwhile
        interval = 5_000;
        setMode("polling");
        schedulePoll();
        return;
      }
      if (isTerminal(j)) return finish();
      openStream();
      schedulePoll();
    })();

    return () => {
      stop();
      ctrl.abort();
    };
  }, [id, nonce]);

  const restart = useCallback(() => {
    setMode("connecting");
    setNonce((n) => n + 1);
  }, []);

  return { job, setJob, error, logs, mode, restart };
}
