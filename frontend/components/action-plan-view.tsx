"use client";

/**
 * An action plan (lookups and writes, core/action_plans.py) made readable: inputs, the browser steps in order
 * with their target alternatives, the session/login block, the captcha step (the only place AI can run), the
 * commit boundary, outputs and the verify action.
 */
import { useState, type ReactNode } from "react";

type Dict = Record<string, unknown>;
type Step = Dict & { id?: string; type: string; commit?: boolean; uses_ai?: boolean };
export type ActionPlan = Dict & {
  kind: "action";
  mode?: string;
  name?: string;
  inputs?: Record<string, Dict>;
  auth?: Record<string, Dict>;
  steps?: Step[];
  output?: Record<string, Dict>;
  verify?: { action?: string; input?: Dict };
  block?: string[];
};

function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-subtle/10 px-1 py-0.5 font-mono text-[12px]">{children}</code>;
}

function Badge({ tone, children }: { tone: "ai" | "commit" | "plain"; children: ReactNode }) {
  const cls =
    tone === "ai"
      ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
      : tone === "commit"
        ? "bg-red-500/15 text-red-700 dark:text-red-300"
        : "bg-subtle/15 text-muted";
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>{children}</span>;
}

/** {"text": "Login"} / [{"css": "#a"}, {"label": "B"}] / {"any": [...], "in": [...]} → readable alternatives. */
function targetText(t: unknown): string {
  if (t == null) return "";
  if (Array.isArray(t)) return t.map(targetText).join("  ·or·  ");
  if (typeof t !== "object") return String(t);
  const d = t as Dict;
  const scope = Array.isArray(d.in) && d.in.length ? ` (in ${d.in.map(targetText).join(" › ")})` : "";
  if (Array.isArray(d.any)) return d.any.map(targetText).join("  ·or·  ") + scope;
  const parts = Object.entries(d)
    .filter(([k]) => k !== "in")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return parts.join(" ") + scope;
}

function stepLine(st: Step): ReactNode {
  switch (st.type) {
    case "browser": {
      const what = String(st.do ?? "");
      return (
        <>
          <span className="font-medium">{what}</span>
          {typeof st.url === "string" && <> <Mono>{st.url}</Mono></>}
          {st.target !== undefined && <> → <Mono>{targetText(st.target)}</Mono></>}
          {typeof st.value === "string" && <> with <Mono>{st.value}</Mono></>}
          {typeof st.label === "string" && <> option <Mono>{st.label}</Mono></>}
          {typeof st.file === "string" && <> file <Mono>{st.file}</Mono></>}
          {typeof st.key === "string" && what === "press" && <> key <Mono>{st.key}</Mono></>}
        </>
      );
    }
    case "session":
      return (
        <>
          <span className="font-medium">session</span>: reuse the stored login for <Mono>{String(st.key ?? "")}</Mono>,
          otherwise log in:
        </>
      );
    case "captcha":
      return (
        <>
          <span className="font-medium">captcha</span>: read <Mono>{targetText(st.image)}</Mono> into{" "}
          <Mono>{targetText(st.into)}</Mono> ({String(st.attempts ?? 1)} attempt(s); non-AI solver first)
        </>
      );
    case "extract":
      return (
        <>
          <span className="font-medium">extract</span> <Mono>{JSON.stringify(Object.fromEntries(Object.entries(st).filter(([k]) => !["id", "type", "note"].includes(k))))}</Mono>
        </>
      );
    case "expect":
      return (
        <>
          <span className="font-medium">expect</span> <Mono>{targetText(st.visible ?? st)}</Mono>
        </>
      );
    case "any_of":
      return <span className="font-medium">first of these that works:</span>;
    default:
      return <span className="font-medium">{st.type}</span>;
  }
}

function StepList({ steps, nested = false }: { steps: Step[]; nested?: boolean }) {
  return (
    <ol className={`space-y-1.5 ${nested ? "ml-5 mt-1.5 border-l border-border pl-3" : ""}`}>
      {steps.map((st, i) => {
        const children = (st.login ?? st.alternatives ?? st.steps) as Step[] | undefined;
        return (
          <li key={`${st.id ?? i}`} className="text-sm">
            <div className="flex flex-wrap items-baseline gap-1.5">
              {!nested && <span className="w-5 shrink-0 text-xs text-muted">{i + 1}.</span>}
              {st.id && <span className="text-xs text-muted">{st.id}</span>}
              <span className="min-w-0 break-words">{stepLine(st)}</span>
              {(st.uses_ai || st.type === "captcha") && <Badge tone="ai">may use AI</Badge>}
              {st.commit && <Badge tone="commit">COMMIT · point of no return</Badge>}
              {typeof st.note === "string" && <span className="text-xs text-muted">— {st.note}</span>}
            </div>
            {Array.isArray(children) && children.length > 0 && <StepList steps={children} nested />}
          </li>
        );
      })}
    </ol>
  );
}

function FieldTable({ title, fields }: { title: string; fields?: Record<string, Dict> }) {
  const rows = Object.entries(fields ?? {});
  if (!rows.length) return null;
  return (
    <div>
      <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">{title}</h4>
      <table className="w-full text-sm">
        <tbody>
          {rows.map(([name, f]) => (
            <tr key={name} className="border-t border-border">
              <td className="py-1 pr-3 align-top"><Mono>{name}</Mono></td>
              <td className="py-1 text-muted">
                {[f.type, f.format, f.required ? "required" : null, f.from ? `from ${String(f.from)}` : null, f.description]
                  .filter(Boolean)
                  .join(" · ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ActionPlanView({ plan }: { plan: ActionPlan }) {
  const [raw, setRaw] = useState(false);
  const json = JSON.stringify(plan, null, 2);
  return (
    <div className="space-y-4 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{plan.name ?? "Action plan"}</span>
        <Badge tone="plain">{plan.mode ?? "action"}</Badge>
        <span className="text-xs text-muted">Runs in a browser from this data; AI only in steps marked “may use AI”.</span>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <FieldTable title="Inputs" fields={plan.inputs} />
        <FieldTable title="Credentials (never stored or logged)" fields={plan.auth} />
      </div>
      <div>
        <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Steps</h4>
        <StepList steps={plan.steps ?? []} />
      </div>
      <FieldTable title="Output" fields={plan.output} />
      {plan.verify?.action && (
        <p className="text-sm">
          Verify afterwards with <Mono>{plan.verify.action}</Mono>
          {plan.verify.input && <> using <Mono>{JSON.stringify(plan.verify.input)}</Mono></>}
        </p>
      )}
      <div className="flex gap-3 text-xs">
        <button type="button" className="text-accent underline" onClick={() => setRaw((v) => !v)}>
          {raw ? "Hide raw JSON" : "Show raw JSON"}
        </button>
        <button type="button" className="text-accent underline" onClick={() => navigator.clipboard?.writeText(json)}>
          Copy JSON
        </button>
      </div>
      {raw && <pre className="max-h-96 overflow-auto rounded bg-subtle/10 p-3 text-[12px]">{json}</pre>}
    </div>
  );
}
