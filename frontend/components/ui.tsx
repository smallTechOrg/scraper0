"use client";

import Link from "next/link";
import {
  forwardRef,
  useEffect,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { errorMessage, type JobStatus } from "@/lib/api";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

// ---------------------------------------------------------------------------
// Buttons
// ---------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-sm",
  secondary: "border border-border-strong bg-surface text-fg hover:bg-surface-2 shadow-sm",
  ghost: "text-muted hover:bg-surface-2 hover:text-fg",
  danger: "bg-danger text-white hover:opacity-90 shadow-sm dark:text-black",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-sm gap-1.5",
  md: "h-10 px-4 text-sm gap-2",
};

export function buttonClass(variant: Variant = "secondary", size: Size = "md", extra?: string) {
  return cx(
    "inline-flex shrink-0 items-center justify-center rounded-md font-medium transition-colors",
    "disabled:cursor-not-allowed disabled:opacity-50",
    variants[variant],
    sizes[size],
    extra,
  );
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={buttonClass(variant, size, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && <Spinner className="size-4" />}
      {children}
    </button>
  );
});

export function ButtonLink({
  href,
  variant = "secondary",
  size = "md",
  className,
  children,
}: {
  href: string;
  variant?: Variant;
  size?: Size;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={buttonClass(variant, size, className)}>
      {children}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Form controls
// ---------------------------------------------------------------------------

const controlBase =
  "w-full rounded-md border border-border-strong bg-surface px-3 text-sm text-fg placeholder:text-subtle " +
  "focus:border-accent focus:outline-none focus:ring-2 focus:ring-ring/30 disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...rest },
  ref,
) {
  return <input ref={ref} className={cx(controlBase, "h-10", className)} {...rest} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return <textarea ref={ref} className={cx(controlBase, "min-h-24 py-2", className)} {...rest} />;
  },
);

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select(
  { className, children, ...rest },
  ref,
) {
  return (
    <select ref={ref} className={cx(controlBase, "h-10 pr-8", className)} {...rest}>
      {children}
    </select>
  );
});

/** Label + control + hint/error. Pass a render function to receive the id. */
export function Field({
  label,
  hint,
  error,
  children,
  className,
  optional,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: (id: string, describedBy?: string) => ReactNode;
  className?: string;
  optional?: boolean;
}) {
  const id = useId();
  const hintId = hint || error ? `${id}-hint` : undefined;
  return (
    <div className={cx("space-y-1.5", className)}>
      <label htmlFor={id} className="block text-sm font-medium text-fg">
        {label}
        {optional && <span className="ml-1 font-normal text-subtle">(optional)</span>}
      </label>
      {children(id, hintId)}
      {error ? (
        <p id={hintId} className="text-xs text-danger">
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-subtle">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function Checkbox({
  label,
  checked,
  onChange,
  hint,
  disabled,
}: {
  label: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2.5">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 size-4 rounded border-border-strong accent-[var(--accent)]"
      />
      <label htmlFor={id} className="text-sm">
        <span className="font-medium">{label}</span>
        {hint && <span className="block text-xs text-subtle">{hint}</span>}
      </label>
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-50",
        checked ? "bg-accent" : "bg-border-strong",
      )}
    >
      <span
        className={cx(
          "inline-block size-4 rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Layout bits
// ---------------------------------------------------------------------------

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("rounded-lg border border-border bg-surface shadow-sm", className)}>{children}</div>;
}

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  back,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <div className="mb-6">
      {back && (
        <Link href={back.href} className="mb-2 inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
          <span aria-hidden>←</span> {back.label}
        </Link>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
          {description && <p className="mt-1 max-w-2xl text-sm text-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "success" | "danger" | "warning";
  className?: string;
}) {
  const tones = {
    neutral: "bg-surface-2 text-muted border-border",
    accent: "bg-accent-soft text-accent-soft-fg border-transparent",
    success: "bg-success-soft text-success-soft-fg border-transparent",
    danger: "bg-danger-soft text-danger-soft-fg border-transparent",
    warning: "bg-warning-soft text-warning-soft-fg border-transparent",
  } as const;
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

const statusTone: Record<string, "neutral" | "accent" | "success" | "danger" | "warning"> = {
  queued: "neutral",
  running: "accent",
  succeeded: "success",
  failed: "danger",
  cancelled: "warning",
};

export function StatusBadge({ status }: { status: JobStatus | string }) {
  return (
    <Badge tone={statusTone[status] ?? "neutral"}>
      {status === "running" && <span className="size-1.5 animate-pulse rounded-full bg-current" aria-hidden />}
      {status}
    </Badge>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cx("animate-spin", className ?? "size-5")} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Data states
// ---------------------------------------------------------------------------

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 px-4 py-10 text-sm text-muted">
      <Spinner className="size-4" />
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="mx-auto mt-1 max-w-md text-sm text-muted">{description}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div role="alert" className="px-4 py-8 text-center">
      <p className="text-sm font-medium text-danger">Couldn&apos;t load this</p>
      <p className="mt-1 text-sm text-muted">{errorMessage(error)}</p>
      {onRetry && (
        <Button size="sm" className="mt-3" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function Alert({
  tone = "danger",
  children,
  className,
}: {
  tone?: "danger" | "success" | "warning" | "accent";
  children: ReactNode;
  className?: string;
}) {
  const tones = {
    danger: "bg-danger-soft text-danger-soft-fg",
    success: "bg-success-soft text-success-soft-fg",
    warning: "bg-warning-soft text-warning-soft-fg",
    accent: "bg-accent-soft text-accent-soft-fg",
  } as const;
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cx("rounded-md px-3 py-2 text-sm", tones[tone], className)}>
      {children}
    </div>
  );
}

/**
 * Render loading / error / empty / content for an async value. `isEmpty`
 * defaults to "array with no entries".
 */
export function DataView<T>({
  state,
  empty,
  isEmpty,
  children,
}: {
  state: { data: T | undefined; error: unknown; loading: boolean; reload: () => void };
  empty?: ReactNode;
  isEmpty?: (d: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (state.data === undefined) {
    if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
    return <LoadingState />;
  }
  const emptyCheck = isEmpty ?? ((d: T) => Array.isArray(d) && d.length === 0);
  return (
    <>
      {state.error ? (
        <div className="px-4 pt-3">
          <Alert>
            {errorMessage(state.error)}{" "}
            <button className="underline" onClick={state.reload}>
              Retry
            </button>
          </Alert>
        </div>
      ) : null}
      {emptyCheck(state.data) && empty ? empty : children(state.data)}
    </>
  );
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("overflow-x-auto", className)}>
      <table className="w-full min-w-full border-collapse text-left text-sm">{children}</table>
    </div>
  );
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={cx(
        "whitespace-nowrap border-b border-border bg-surface-2 px-4 py-2 text-xs font-medium uppercase tracking-wide text-subtle",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return <td className={cx("border-b border-border px-4 py-2.5 align-top", className)}>{children}</td>;
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

export function CopyButton({ value, label = "Copy", size = "sm" }: { value: string; label?: string; size?: Size }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <Button
      size={size}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
        } catch {
          // clipboard blocked (insecure context) — the value is still selectable on screen
        }
      }}
      aria-live="polite"
    >
      {copied ? "Copied" : label}
    </Button>
  );
}

/** A read-only value with a copy button (invite URLs, API keys). */
export function CopyField({ value, label }: { value: string; label?: string }) {
  return (
    <div className="flex items-stretch gap-2">
      <input
        readOnly
        value={value}
        aria-label={label ?? "Value"}
        onFocus={(e) => e.currentTarget.select()}
        className={cx(controlBase, "h-10 font-mono text-xs")}
      />
      <CopyButton value={value} size="md" />
    </div>
  );
}

export function CodeBlock({ code, label }: { code: string; label?: string }) {
  return (
    <div className="relative">
      <pre
        aria-label={label}
        className="overflow-x-auto rounded-md border border-border bg-surface-2 p-3 pr-20 font-mono text-xs leading-relaxed text-fg"
      >
        <code>{code}</code>
      </pre>
      <div className="absolute right-2 top-2">
        <CopyButton value={code} />
      </div>
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-subtle">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  );
}

/** Horizontal bar showing spend against a budget. */
export function BudgetBar({ spend, budget }: { spend: number; budget: number | null | undefined }) {
  if (!budget || budget <= 0) {
    return <p className="text-xs text-subtle">No monthly budget set</p>;
  }
  const pct = Math.min(100, (spend / budget) * 100);
  const tone = pct >= 100 ? "bg-danger" : pct >= 80 ? "bg-[#d97706]" : "bg-accent";
  return (
    <div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-surface-2"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={budget}
        aria-valuenow={spend}
        aria-label="AI spend against budget"
      >
        <div className={cx("h-full rounded-full", tone)} style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 text-xs text-muted tabular-nums">{pct.toFixed(0)}% of budget used</p>
    </div>
  );
}
