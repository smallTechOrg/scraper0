"use client";

import { useState, type FormEvent } from "react";
import { Modal } from "@/components/modal";
import { parseBudget, slugify, SLUG_RE } from "@/components/tenant-fields";
import {
  Alert,
  Badge,
  BudgetBar,
  Button,
  Card,
  CopyField,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
  Select,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, errorMessage, type AdminTenant, type TenantRole } from "@/lib/api";
import { formatDate, usd } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

function CreateTenantModal({ onClose, onCreated }: { onClose: () => void; onCreated: (t: AdminTenant) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [budget, setBudget] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ tenant: AdminTenant; invite_url?: string | null } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Name is required.");
    if (!SLUG_RE.test(slug)) return setError("Slug must be lowercase letters, digits and dashes.");
    const b = parseBudget(budget);
    if (b instanceof Error) return setError(b.message);
    setBusy(true);
    try {
      const r = await api.admin.createTenant({
        name: name.trim(),
        slug,
        monthly_ai_budget_usd: b,
        admin_email: email.trim() || undefined,
      });
      setResult(r);
      onCreated(r.tenant);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <Modal
        open
        onClose={onClose}
        title={`${result.tenant.name} created`}
        footer={
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        {result.invite_url ? (
          <div className="space-y-3">
            <p className="text-sm text-muted">Send this invite link to {email}. They&apos;ll join as the tenant admin.</p>
            <CopyField value={result.invite_url} label="Invite URL" />
          </div>
        ) : (
          <p className="text-sm text-muted">No admin was invited. You can invite members from the tenant dashboard.</p>
        )}
      </Modal>
    );
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Create tenant"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="tenant-form" variant="primary" loading={busy}>
            Create tenant
          </Button>
        </>
      }
    >
      <form id="tenant-form" onSubmit={submit} className="space-y-4">
        <Field label="Name">
          {(id) => (
            <Input
              id={id}
              data-autofocus
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!slugTouched) setSlug(slugify(e.target.value));
              }}
            />
          )}
        </Field>
        <Field label="Slug" hint="Short, unique identifier.">
          {(id, hint) => (
            <Input
              id={id}
              aria-describedby={hint}
              className="font-mono"
              value={slug}
              onChange={(e) => {
                setSlugTouched(true);
                setSlug(e.target.value.toLowerCase());
              }}
            />
          )}
        </Field>
        <Field label="Monthly AI budget (USD)" optional>
          {(id) => <Input id={id} type="number" min={0} step="any" value={budget} onChange={(e) => setBudget(e.target.value)} />}
        </Field>
        <Field label="Admin email" optional hint="Creates an invite link for the tenant's first admin.">
          {(id, hint) => <Input id={id} type="email" aria-describedby={hint} value={email} onChange={(e) => setEmail(e.target.value)} />}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

function EditTenantModal({
  tenant,
  onClose,
  onSaved,
}: {
  tenant: AdminTenant;
  onClose: () => void;
  onSaved: (t: AdminTenant) => void;
}) {
  const [name, setName] = useState(tenant.name);
  const [status, setStatus] = useState(tenant.status);
  const [budget, setBudget] = useState(tenant.monthly_ai_budget_usd != null ? String(tenant.monthly_ai_budget_usd) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Name is required.");
    const b = parseBudget(budget);
    if (b instanceof Error) return setError(b.message);
    setBusy(true);
    try {
      const t = await api.admin.updateTenant(tenant.id, { name: name.trim(), status, monthly_ai_budget_usd: b ?? null });
      onSaved({ ...tenant, ...t });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Edit ${tenant.name}`}
      description={<span className="font-mono">{tenant.slug}</span>}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="edit-tenant-form" variant="primary" loading={busy}>
            Save
          </Button>
        </>
      }
    >
      <form id="edit-tenant-form" onSubmit={submit} className="space-y-4">
        <Field label="Name">{(id) => <Input id={id} data-autofocus value={name} onChange={(e) => setName(e.target.value)} />}</Field>
        <Field label="Status" hint="Suspended tenants can't sign in or run jobs.">
          {(id, hint) => (
            <Select id={id} aria-describedby={hint} value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="active">active</option>
              <option value="suspended">suspended</option>
              {!["active", "suspended"].includes(tenant.status) && <option value={tenant.status}>{tenant.status}</option>}
            </Select>
          )}
        </Field>
        <Field label="Monthly AI budget (USD)" hint="Leave blank for no cap.">
          {(id, hint) => (
            <Input id={id} type="number" min={0} step="any" aria-describedby={hint} value={budget} onChange={(e) => setBudget(e.target.value)} />
          )}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

function InviteToTenantModal({ tenant, onClose }: { tenant: AdminTenant; onClose: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<TenantRole>("admin");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim()) return setError("Enter an email address.");
    setBusy(true);
    setError(null);
    try {
      setUrl((await api.admin.inviteToTenant(tenant.id, email.trim(), role)).invite_url);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={url ? "Invite created" : `Invite to ${tenant.name}`}
      description={url ? `Send this link to ${email}. It works once.` : undefined}
      footer={
        url ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form="tenant-invite-form" variant="primary" loading={busy}>
              Create invite
            </Button>
          </>
        )
      }
    >
      {url ? (
        <CopyField value={url} label="Invite URL" />
      ) : (
        <form id="tenant-invite-form" onSubmit={submit} className="space-y-4">
          <Field label="Email">
            {(id) => <Input id={id} type="email" data-autofocus value={email} onChange={(e) => setEmail(e.target.value)} />}
          </Field>
          <Field label="Role">
            {(id) => (
              <Select id={id} value={role} onChange={(e) => setRole(e.target.value as TenantRole)}>
                <option value="admin">Admin</option>
                <option value="member">Member</option>
              </Select>
            )}
          </Field>
          {error && <Alert>{error}</Alert>}
        </form>
      )}
    </Modal>
  );
}

export default function AdminTenantsPage() {
  const tenants = useAsync(() => api.admin.tenants(), "admin-tenants");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminTenant | null>(null);
  const [inviting, setInviting] = useState<AdminTenant | null>(null);

  const upsert = (t: AdminTenant) =>
    tenants.setData((prev) => {
      const list = prev ?? [];
      return list.some((x) => x.id === t.id) ? list.map((x) => (x.id === t.id ? t : x)) : [t, ...list];
    });

  return (
    <>
      <PageHeader
        title="Tenants"
        description="Every workspace on this scraper0 instance."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            Create tenant
          </Button>
        }
      />
      <Card>
        <DataView
          state={tenants}
          empty={
            <EmptyState
              title="No tenants yet"
              description="Create one, or approve an access request."
              action={
                <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                  Create tenant
                </Button>
              }
            />
          }
        >
          {(list) => (
            <Table>
              <thead>
                <tr>
                  <Th>Tenant</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Members</Th>
                  <Th>AI spend this month</Th>
                  <Th>Created</Th>
                  <Th>
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.map((t) => (
                  <tr key={t.id} className="hover:bg-surface-2/60">
                    <Td>
                      <p className="font-medium">{t.name}</p>
                      <p className="font-mono text-xs text-subtle">{t.slug}</p>
                    </Td>
                    <Td>
                      <Badge tone={t.status === "active" ? "success" : "warning"}>{t.status}</Badge>
                    </Td>
                    <Td className="text-right tabular-nums">{t.members}</Td>
                    <Td className="min-w-[12rem]">
                      <p className="mb-1 text-sm tabular-nums">
                        {usd(t.ai_spend_this_month_usd, 2)}
                        <span className="text-muted"> / {t.monthly_ai_budget_usd ? usd(t.monthly_ai_budget_usd, 2) : "no cap"}</span>
                      </p>
                      {t.monthly_ai_budget_usd ? (
                        <BudgetBar spend={t.ai_spend_this_month_usd} budget={t.monthly_ai_budget_usd} />
                      ) : null}
                    </Td>
                    <Td className="whitespace-nowrap text-muted">{formatDate(t.created_at, false)}</Td>
                    <Td className="text-right">
                      <div className="flex justify-end gap-1.5">
                        <Button size="sm" onClick={() => setInviting(t)}>
                          Invite
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(t)}>
                          Edit
                        </Button>
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </DataView>
      </Card>
      {inviting && <InviteToTenantModal tenant={inviting} onClose={() => setInviting(null)} />}
      {creating && <CreateTenantModal onClose={() => setCreating(false)} onCreated={upsert} />}
      {editing && (
        <EditTenantModal
          tenant={editing}
          onClose={() => setEditing(null)}
          onSaved={(t) => {
            upsert(t);
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
