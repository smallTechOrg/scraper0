"use client";

import { useState, type FormEvent } from "react";
import {
  errorMessage,
  PROVIDER_KINDS,
  type ProviderCreate,
  type ProviderKind,
  type ProviderPatch,
  type ProviderPublic,
  type ProviderTestResult,
} from "@/lib/api";
import { useConfirm } from "./confirm";
import { Modal } from "./modal";
import { Alert, Badge, Button, EmptyState, Field, Input, Select, Switch } from "./ui";

export interface ProviderOps {
  create: (body: ProviderCreate) => Promise<ProviderPublic>;
  update: (id: string, body: ProviderPatch) => Promise<ProviderPublic>;
  remove: (id: string) => Promise<void>;
  test: (id: string) => Promise<ProviderTestResult>;
}

const KIND_LABEL: Record<ProviderKind, string> = {
  groq: "Groq",
  anthropic: "Anthropic",
  openai_compat: "OpenAI-compatible",
};

const MODEL_PLACEHOLDER: Record<ProviderKind, { model: string; vision: string }> = {
  groq: { model: "meta-llama/llama-4-scout-17b-16e-instruct", vision: "same as model" },
  anthropic: { model: "claude-model-id", vision: "same as model" },
  openai_compat: { model: "model-name", vision: "vision-model-name" },
};

export function ProviderEditor({
  provider,
  ops,
  onClose,
  onSaved,
}: {
  provider: ProviderPublic | null;
  ops: ProviderOps;
  onClose: () => void;
  onSaved: (p: ProviderPublic) => void;
}) {
  const [name, setName] = useState(provider?.name ?? "");
  const [kind, setKind] = useState<ProviderKind>(provider?.kind ?? "groq");
  const [model, setModel] = useState(provider?.model ?? "");
  const [vision, setVision] = useState(provider?.vision_model ?? "");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [priceIn, setPriceIn] = useState(provider?.price_in_per_mtok != null ? String(provider.price_in_per_mtok) : "");
  const [priceOut, setPriceOut] = useState(provider?.price_out_per_mtok != null ? String(provider.price_out_per_mtok) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Name is required.");
    if (!model.trim()) return setError("Model is required.");
    if (!provider && !apiKey.trim()) return setError("API key is required.");
    if (kind === "openai_compat" && !baseUrl.trim()) return setError("Base URL is required for OpenAI-compatible providers.");
    const num = (s: string, label: string): number | undefined | Error => {
      if (!s.trim()) return undefined;
      const n = Number(s);
      return Number.isFinite(n) && n >= 0 ? n : new Error(`${label} must be a non-negative number.`);
    };
    const pin = num(priceIn, "Input price");
    const pout = num(priceOut, "Output price");
    if (pin instanceof Error) return setError(pin.message);
    if (pout instanceof Error) return setError(pout.message);

    setBusy(true);
    try {
      let saved: ProviderPublic;
      if (provider) {
        const patch: ProviderPatch = {
          name: name.trim(),
          model: model.trim(),
          vision_model: vision.trim() || undefined,
          base_url: kind === "openai_compat" ? baseUrl.trim() : undefined,
          price_in_per_mtok: pin,
          price_out_per_mtok: pout,
        };
        if (apiKey.trim()) patch.api_key = apiKey.trim();
        saved = await ops.update(provider.id, patch);
      } else {
        saved = await ops.create({
          name: name.trim(),
          kind,
          model: model.trim(),
          vision_model: vision.trim() || undefined,
          base_url: kind === "openai_compat" ? baseUrl.trim() : undefined,
          api_key: apiKey.trim(),
          price_in_per_mtok: pin,
          price_out_per_mtok: pout,
        });
      }
      onSaved(saved);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={provider ? `Edit ${provider.name}` : "Add AI provider"}
      description="Keys are encrypted at rest and never shown again — only the last few characters."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="provider-form" variant="primary" loading={busy}>
            {provider ? "Save" : "Add provider"}
          </Button>
        </>
      }
    >
      <form id="provider-form" onSubmit={submit} className="space-y-4" autoComplete="off">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name">
            {(id) => <Input id={id} data-autofocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Team Groq" />}
          </Field>
          <Field label="Kind" hint={provider ? "Can't be changed after creation." : undefined}>
            {(id, hint) => (
              <Select
                id={id}
                aria-describedby={hint}
                value={kind}
                disabled={!!provider}
                onChange={(e) => setKind(e.target.value as ProviderKind)}
              >
                {PROVIDER_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {KIND_LABEL[k]}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Model">
            {(id) => (
              <Input id={id} className="font-mono text-xs" value={model} placeholder={MODEL_PLACEHOLDER[kind].model} onChange={(e) => setModel(e.target.value)} />
            )}
          </Field>
          <Field label="Vision model" optional hint="Used for posters, images and captchas.">
            {(id, hint) => (
              <Input
                id={id}
                className="font-mono text-xs"
                aria-describedby={hint}
                value={vision}
                placeholder={MODEL_PLACEHOLDER[kind].vision}
                onChange={(e) => setVision(e.target.value)}
              />
            )}
          </Field>
        </div>
        {kind === "openai_compat" && (
          <Field label="Base URL" hint="The OpenAI-compatible endpoint, e.g. https://host/v1">
            {(id, hint) => (
              <Input id={id} type="url" aria-describedby={hint} className="font-mono text-xs" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
            )}
          </Field>
        )}
        <Field
          label="API key"
          hint={provider ? `Leave blank to keep the current key${provider.key_hint ? ` (${provider.key_hint})` : ""}.` : undefined}
        >
          {(id, hint) => (
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              aria-describedby={hint}
              className="font-mono text-xs"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          )}
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Input price" optional hint="USD per million tokens.">
            {(id, hint) => (
              <Input id={id} type="number" min={0} step="any" aria-describedby={hint} value={priceIn} onChange={(e) => setPriceIn(e.target.value)} />
            )}
          </Field>
          <Field label="Output price" optional hint="USD per million tokens.">
            {(id, hint) => (
              <Input id={id} type="number" min={0} step="any" aria-describedby={hint} value={priceOut} onChange={(e) => setPriceOut(e.target.value)} />
            )}
          </Field>
        </div>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

function ProviderRow({
  p,
  editable,
  ops,
  onChange,
  onRemoved,
  onEdit,
  active,
}: {
  p: ProviderPublic;
  editable: boolean;
  ops?: ProviderOps;
  onChange?: (p: ProviderPublic) => void;
  onRemoved?: (id: string) => void;
  onEdit?: (p: ProviderPublic) => void;
  active?: boolean;
}) {
  const confirm = useConfirm();
  const [test, setTest] = useState<{ busy: boolean; result?: ProviderTestResult; error?: string }>({ busy: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function patch(body: ProviderPatch) {
    if (!ops) return;
    setBusy(true);
    setError(null);
    try {
      onChange?.(await ops.update(p.id, body));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    if (!ops) return;
    setTest({ busy: true });
    try {
      setTest({ busy: false, result: await ops.test(p.id) });
    } catch (err) {
      setTest({ busy: false, error: errorMessage(err) });
    }
  }

  async function remove() {
    if (!ops) return;
    const ok = await confirm({
      title: `Delete ${p.name}?`,
      message: "The encrypted key is destroyed. Jobs fall back to the next available provider.",
      confirmLabel: "Delete provider",
      action: () => ops.remove(p.id),
    });
    if (ok) onRemoved?.(p.id);
  }

  return (
    <li className="px-4 py-4 sm:px-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{p.name}</span>
            <Badge>{KIND_LABEL[p.kind] ?? p.kind}</Badge>
            {p.is_default && <Badge tone="accent">default</Badge>}
            {active && <Badge tone="success">in use</Badge>}
            {!p.enabled && <Badge tone="warning">disabled</Badge>}
          </p>
          <p className="mt-1 break-all font-mono text-xs text-muted">
            {p.model}
            {p.vision_model && p.vision_model !== p.model && <> · vision: {p.vision_model}</>}
          </p>
          <p className="mt-0.5 text-xs text-subtle">
            {p.key_hint ? `key ${p.key_hint}` : "no key"}
            {p.base_url && <> · {p.base_url}</>}
            {(p.price_in_per_mtok != null || p.price_out_per_mtok != null) && (
              <>
                {" "}
                · ${p.price_in_per_mtok ?? "?"} in / ${p.price_out_per_mtok ?? "?"} out per Mtok
              </>
            )}
          </p>
        </div>
        {editable && ops && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-2 text-xs text-muted">
              <Switch
                checked={p.enabled}
                disabled={busy}
                label={`${p.enabled ? "Disable" : "Enable"} ${p.name}`}
                onChange={(v) => patch({ enabled: v })}
              />
              {p.enabled ? "Enabled" : "Disabled"}
            </span>
            <Button size="sm" onClick={runTest} loading={test.busy}>
              Test
            </Button>
            {!p.is_default && (
              <Button size="sm" onClick={() => patch({ is_default: true })} disabled={busy || !p.enabled}>
                Make default
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => onEdit?.(p)}>
              Edit
            </Button>
            <Button size="sm" variant="ghost" className="hover:text-danger" onClick={remove}>
              Delete
            </Button>
          </div>
        )}
      </div>
      {test.result && (
        <Alert tone={test.result.ok ? "success" : "danger"} className="mt-3">
          {test.result.ok
            ? `Connection OK${test.result.latency_ms != null ? ` · ${Math.round(test.result.latency_ms)} ms` : ""}`
            : `Test failed: ${test.result.error ?? "unknown error"}`}
        </Alert>
      )}
      {test.error && <Alert className="mt-3">{test.error}</Alert>}
      {error && <Alert className="mt-3">{error}</Alert>}
    </li>
  );
}

/** List of providers; editable when `ops` is given. */
export function ProviderList({
  providers,
  ops,
  activeId,
  onChange,
  emptyTitle = "No providers",
  emptyDescription,
  emptyAction,
}: {
  providers: ProviderPublic[];
  ops?: ProviderOps;
  activeId?: string | null;
  onChange?: (next: ProviderPublic[]) => void;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: React.ReactNode;
}) {
  const [editing, setEditing] = useState<ProviderPublic | null>(null);

  const replace = (p: ProviderPublic) => {
    // A new default demotes the others.
    const next = providers.map((x) => (x.id === p.id ? p : p.is_default ? { ...x, is_default: false } : x));
    onChange?.(next);
  };

  if (providers.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />;
  }

  return (
    <>
      <ul className="divide-y divide-border">
        {providers.map((p) => (
          <ProviderRow
            key={p.id}
            p={p}
            editable={!!ops}
            ops={ops}
            active={activeId === p.id}
            onChange={replace}
            onRemoved={(id) => onChange?.(providers.filter((x) => x.id !== id))}
            onEdit={setEditing}
          />
        ))}
      </ul>
      {editing && ops && (
        <ProviderEditor
          provider={editing}
          ops={ops}
          onClose={() => setEditing(null)}
          onSaved={(p) => {
            replace(p);
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
