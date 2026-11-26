"use client";

import { useState, type FormEvent } from "react";
import { useConfirm } from "@/components/confirm";
import { Modal } from "@/components/modal";
import { AdminOnly, useSession } from "@/components/session";
import {
  Alert,
  Badge,
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
import { api, errorMessage, type Member, type TenantRole } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

function InviteModal({ onClose }: { onClose: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<TenantRole>("member");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim()) return setError("Enter an email address.");
    setBusy(true);
    setError(null);
    try {
      const r = await api.inviteMember(email.trim(), role);
      setUrl(r.invite_url);
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
      title={url ? "Invite created" : "Invite a member"}
      description={url ? `Send this link to ${email}. It works once.` : "They'll get a link to set their name and password."}
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
            <Button type="submit" form="invite-form" variant="primary" loading={busy}>
              Create invite
            </Button>
          </>
        )
      }
    >
      {url ? (
        <CopyField value={url} label="Invite URL" />
      ) : (
        <form id="invite-form" onSubmit={submit} className="space-y-4">
          <Field label="Email">
            {(id) => <Input id={id} type="email" data-autofocus value={email} onChange={(e) => setEmail(e.target.value)} />}
          </Field>
          <Field label="Role" hint="Admins manage keys, sources, AI providers and members. Members can view everything and run jobs.">
            {(id, hint) => (
              <Select id={id} aria-describedby={hint} value={role} onChange={(e) => setRole(e.target.value as TenantRole)}>
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </Select>
            )}
          </Field>
          {error && <Alert>{error}</Alert>}
        </form>
      )}
    </Modal>
  );
}

export default function MembersPage() {
  const { canAdmin, user } = useSession();
  const confirm = useConfirm();
  const members = useAsync(() => api.members(), "members");
  const [inviting, setInviting] = useState(false);

  async function remove(m: Member) {
    const self = m.user_id === user.id;
    const ok = await confirm({
      title: self ? "Leave this workspace?" : `Remove ${m.name || m.email}?`,
      message: self
        ? "You'll lose access to this workspace until someone invites you again."
        : "They lose access to this workspace immediately. Their past jobs are kept.",
      confirmLabel: self ? "Leave" : "Remove member",
      action: () => api.removeMember(m.user_id),
    });
    if (ok) members.setData((prev) => (prev ?? []).filter((x) => x.user_id !== m.user_id));
  }

  return (
    <>
      <PageHeader
        title="Members"
        description="People with access to this workspace."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setInviting(true)}>
              Invite member
            </Button>
          </AdminOnly>
        }
      />
      <Card>
        <DataView state={members} empty={<EmptyState title="No members" />}>
          {(list) => (
            <Table>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Email</Th>
                  <Th>Role</Th>
                  <Th>
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.map((m) => (
                  <tr key={m.user_id} className="hover:bg-surface-2/60">
                    <Td className="font-medium">
                      {m.name || "—"}
                      {m.user_id === user.id && <span className="ml-2 text-xs text-subtle">(you)</span>}
                    </Td>
                    <Td className="break-all text-muted">{m.email}</Td>
                    <Td>
                      <Badge tone={m.role === "admin" ? "accent" : "neutral"}>{m.role}</Badge>
                    </Td>
                    <Td className="text-right">
                      {canAdmin && (
                        <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => remove(m)}>
                          {m.user_id === user.id ? "Leave" : "Remove"}
                        </Button>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </DataView>
      </Card>
      {inviting && <InviteModal onClose={() => setInviting(false)} />}
    </>
  );
}
