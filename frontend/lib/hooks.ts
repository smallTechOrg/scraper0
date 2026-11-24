"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
  setData: (updater: T | ((prev: T | undefined) => T)) => void;
}

/**
 * Run an async loader and track loading / error / data. Re-runs when `key`
 * changes or `reload()` is called. Stale responses are ignored.
 */
export function useAsync<T>(loader: () => Promise<T>, key: string = ""): AsyncState<T> {
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  });
  const [state, setState] = useState<{ data: T | undefined; error: unknown; loading: boolean }>({
    data: undefined,
    error: undefined,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let active = true;
    setState((s) => ({ ...s, loading: true, error: undefined }));
    loaderRef.current().then(
      (data) => {
        if (active) setState({ data, error: undefined, loading: false });
      },
      (error: unknown) => {
        if (active) setState((s) => ({ data: s.data, error, loading: false }));
      },
    );
    return () => {
      active = false;
    };
  }, [key, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback((updater: T | ((prev: T | undefined) => T)) => {
    setState((s) => ({
      ...s,
      data: typeof updater === "function" ? (updater as (p: T | undefined) => T)(s.data) : updater,
    }));
  }, []);

  return { ...state, reload, setData };
}
