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
  CodeBlock,
  CopyField,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, errorMessage, type CreatedApiKey } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { useAsync } from "@/lib/hooks";

function CreateKeyModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return setError("Give the key a name so you can recognise it later.");
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.createApiKey(name.trim()));
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <Modal
        open
        onClose={onClose}
        title="Your new API key"
        footer={
          <Button variant="primary" onClick={onClose}>
            I&apos;ve saved it
          </Button>
        }
      >
        <div className="space-y-4">
          <Alert tone="warning">
            Copy this key now. It won&apos;t be shown again — if you lose it, revoke it and create a new one.
          </Alert>
          <CopyField value={created.key} label="API key" />
          <CodeBlock
            label="Example request"
            code={`curl https://your-scraper0-host/v1/jobs \\\n  -H "X-API-Key: ${created.prefix}…"`}
          />
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Create API key"
      description="Keys act on behalf of this workspace. Send them in the X-API-Key header."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="key-form" variant="primary" loading={busy}>
            Create key
          </Button>
        </>
      }
    >
      <form id="key-form" onSubmit={submit} className="space-y-4">
        <Field label="Name" hint="e.g. production backend, nightly sync script">
          {(id, hint) => <Input id={id} data-autofocus aria-describedby={hint} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

export default function ApiKeysPage() {
  const { canAdmin } = useSession();
  const confirm = useConfirm();
  const keys = useAsync(() => api.apiKeys(), "keys");
  const [creating, setCreating] = useState(false);

  async function revoke(id: string, name: string) {
    const ok = await confirm({
      title: `Revoke “${name}”?`,
      message: "Programs using this key will get 401 errors immediately. This can't be undone.",
      confirmLabel: "Revoke key",
      action: () => api.revokeApiKey(id),
    });
    if (ok) keys.reload();
  }

  return (
    <>
      <PageHeader
        title="API keys"
        description="For programs and scripts. Each key is scoped to this workspace."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setCreating(true)}>
              Create key
            </Button>
          </AdminOnly>
        }
      />
      <Card>
        <DataView
          state={keys}
          empty={
            <EmptyState
              title="No API keys"
              description={canAdmin ? "Create a key to call the API from your own code." : "A workspace admin can create API keys."}
              action={
                canAdmin && (
                  <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                    Create key
                  </Button>
                )
              }
            />
          }
        >
          {(list) => (
            <Table>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Key</Th>
                  <Th>Created</Th>
                  <Th>Last used</Th>
                  <Th>
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.map((k) => (
                  <tr key={k.id} className={k.revoked_at ? "opacity-60" : "hover:bg-surface-2/60"}>
                    <Td className="font-medium">
                      {k.name}
                      {k.revoked_at && (
                        <Badge tone="danger" className="ml-2">
                          revoked
                        </Badge>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap font-mono text-xs">{k.prefix}…</Td>
                    <Td className="whitespace-nowrap text-muted">{formatDate(k.created_at, false)}</Td>
                    <Td className="whitespace-nowrap text-muted">{k.last_used_at ? timeAgo(k.last_used_at) : "Never"}</Td>
                    <Td className="text-right">
                      {canAdmin && !k.revoked_at && (
                        <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => revoke(k.id, k.name)}>
                          Revoke
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
      {creating && <CreateKeyModal onClose={() => setCreating(false)} onCreated={keys.reload} />}
    </>
  );
}
