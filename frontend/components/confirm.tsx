"use client";

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { Modal } from "./modal";
import { Alert, Button } from "./ui";
import { errorMessage } from "@/lib/api";

interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  /** Destructive styling (default true). */
  danger?: boolean;
  /**
   * Optional async action run while the dialog shows a spinner. If it throws,
   * the error is shown in the dialog and it stays open.
   */
  action?: () => Promise<unknown>;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((o) => {
    setOpts(o);
    setError(null);
    setBusy(false);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const close = (v: boolean) => {
    if (busy) return;
    resolver.current?.(v);
    resolver.current = null;
    setOpts(null);
  };

  const onConfirm = async () => {
    if (!opts) return;
    if (opts.action) {
      setBusy(true);
      setError(null);
      try {
        await opts.action();
      } catch (err) {
        setError(errorMessage(err));
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    resolver.current?.(true);
    resolver.current = null;
    setOpts(null);
  };

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Modal
        open={!!opts}
        onClose={() => close(false)}
        title={opts?.title ?? ""}
        size="sm"
        footer={
          <>
            <Button onClick={() => close(false)} disabled={busy} data-autofocus>
              Cancel
            </Button>
            <Button variant={opts?.danger === false ? "primary" : "danger"} onClick={onConfirm} loading={busy}>
              {opts?.confirmLabel ?? "Confirm"}
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm text-muted">
          {opts?.message}
          {error && <Alert>{error}</Alert>}
        </div>
      </Modal>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used inside <ConfirmProvider>");
  return ctx;
}
