"use client";

import { useState, type FormEvent } from "react";
import { useConfirm } from "@/components/confirm";
import { Modal } from "@/components/modal";
import { parseBudget, slugify, SLUG_RE } from "@/components/tenant-fields";
import {
  Alert,
  Button,
  Card,
  CopyField,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
} from "@/components/ui";
import { api, errorMessage, type AccessRequest } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

function ApproveModal({
  req,
  onClose,
  onApproved,
}: {
  req: AccessRequest;
  onClose: () => void;
  onApproved: (id: string) => void;
}) {
  const base = req.organisation || req.name;
  const [name, setName] = useState(base);
  const [slug, setSlug] = useState(slugify(base));
  const [budget, setBudget] = useState("10");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invite, setInvite] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!SLUG_RE.test(slug)) return setError("Slug must be lowercase letters, digits and dashes.");
    const b = parseBudget(budget);
    if (b instanceof Error) return setError(b.message);
    setBusy(true);
    try {
      const r = await api.admin.approveAccessRequest(req.id, {
        slug,
        name: name.trim() || undefined,
        monthly_ai_budget_usd: b,
      });
      setInvite(r.invite_url);
      onApproved(req.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (invite) {
    return (
      <Modal
        open
        onClose={onClose}
        title="Approved"
        description={`Send this invite link to ${req.email}. They'll join ${name || slug} as its admin.`}
        footer={
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        <CopyField value={invite} label="Invite URL" />
      </Modal>
    );
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Approve ${req.name}`}
      description="Creates a tenant and an invite for the requester as its admin."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="approve-form" variant="primary" loading={busy}>
            Approve &amp; create tenant
          </Button>
        </>
      }
    >
      <form id="approve-form" onSubmit={submit} className="space-y-4">
        <Field label="Tenant name">{(id) => <Input id={id} value={name} onChange={(e) => setName(e.target.value)} />}</Field>
        <Field label="Slug">
          {(id) => (
            <Input id={id} data-autofocus className="font-mono" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} />
          )}
        </Field>
        <Field label="Monthly AI budget (USD)" optional hint="Leave blank for no cap.">
          {(id, hint) => (
            <Input id={id} type="number" min={0} step="any" aria-describedby={hint} value={budget} onChange={(e) => setBudget(e.target.value)} />
          )}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

export default function AccessRequestsPage() {
  const confirm = useConfirm();
  const requests = useAsync(() => api.admin.accessRequests("pending"), "access-requests");
  const [approving, setApproving] = useState<AccessRequest | null>(null);
  const drop = (id: string) => requests.setData((prev) => (prev ?? []).filter((r) => r.id !== id));

  async function reject(r: AccessRequest) {
    const ok = await confirm({
      title: `Reject ${r.name}?`,
      message: `The request from ${r.email} is marked rejected. No email is sent.`,
      confirmLabel: "Reject request",
      action: () => api.admin.rejectAccessRequest(r.id),
    });
    if (ok) drop(r.id);
  }

  return (
    <>
      <PageHeader title="Access requests" description="People who asked for access on the landing page." />
      <Card>
        <DataView
          state={requests}
          empty={<EmptyState title="No pending requests" description="New requests from the landing page show up here." />}
        >
          {(list) => (
            <ul className="divide-y divide-border">
              {list.map((r) => (
                <li key={r.id} className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-start sm:justify-between sm:px-5">
                  <div className="min-w-0">
                    <p className="font-medium">
                      {r.name}
                      {r.organisation && <span className="font-normal text-muted"> · {r.organisation}</span>}
                    </p>
                    <a href={`mailto:${r.email}`} className="break-all text-sm text-accent hover:underline">
                      {r.email}
                    </a>
                    {r.use_case && <p className="mt-2 whitespace-pre-line text-sm text-muted">{r.use_case}</p>}
                    <p className="mt-1 text-xs text-subtle" title={formatDate(r.created_at)}>
                      {timeAgo(r.created_at)}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" variant="primary" onClick={() => setApproving(r)}>
                      Approve
                    </Button>
                    <Button size="sm" onClick={() => reject(r)}>
                      Reject
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </DataView>
      </Card>
      {approving && <ApproveModal req={approving} onClose={() => setApproving(null)} onApproved={drop} />}
    </>
  );
}
