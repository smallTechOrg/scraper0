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
  CardHeader,
  Checkbox,
  DataView,
  EmptyState,
  Field,
  Input,
  PageHeader,
  Select,
  Table,
  Td,
  Th,
  Textarea,
} from "@/components/ui";
import { api, errorMessage, FIELD_TYPES, type EntityField, type EntitySpec, type FieldType } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

const NAME_RE = /^[a-z][a-z0-9_]{0,62}$/;

interface FieldDraft {
  key: number;
  name: string;
  type: FieldType;
  required: boolean;
  description: string;
  choices: string;
  weight?: number;
}

let draftSeq = 0;
const toDraft = (f: EntityField): FieldDraft => ({
  key: ++draftSeq,
  name: f.name,
  type: f.type,
  required: !!f.required,
  description: f.description ?? "",
  choices: (f.choices ?? []).join(", "),
  weight: f.weight,
});
const blankField = (): FieldDraft => ({ key: ++draftSeq, name: "", type: "text", required: false, description: "", choices: "" });

function FieldsTable({ fields }: { fields: EntityField[] }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Field</Th>
          <Th>Type</Th>
          <Th>Description</Th>
        </tr>
      </thead>
      <tbody>
        {fields.map((f) => (
          <tr key={f.name}>
            <Td className="whitespace-nowrap font-mono text-xs">
              {f.name}
              {f.required && <span className="ml-1 text-danger" title="required">*</span>}
            </Td>
            <Td>
              <Badge>{f.type}</Badge>
              {f.type === "enum" && f.choices?.length ? (
                <span className="ml-2 text-xs text-subtle">{f.choices.join(" · ")}</span>
              ) : null}
            </Td>
            <Td className="text-muted">{f.description || "—"}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function EntityEditor({
  entity,
  existing,
  onClose,
  onSaved,
}: {
  entity: EntitySpec | null;
  existing: string[];
  onClose: () => void;
  onSaved: (e: EntitySpec) => void;
}) {
  const [name, setName] = useState(entity?.name ?? "");
  const [description, setDescription] = useState(entity?.description ?? "");
  const [fields, setFields] = useState<FieldDraft[]>(entity ? entity.fields.map(toDraft) : [blankField()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const update = (key: number, patch: Partial<FieldDraft>) =>
    setFields((fs) => fs.map((f) => (f.key === key ? { ...f, ...patch } : f)));
  const move = (i: number, d: -1 | 1) =>
    setFields((fs) => {
      const j = i + d;
      if (j < 0 || j >= fs.length) return fs;
      const next = [...fs];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const n = name.trim();
    if (!entity) {
      if (!NAME_RE.test(n)) return setError("Name must be lowercase letters, digits and underscores, starting with a letter.");
      if (existing.includes(n)) return setError(`An entity called “${n}” already exists.`);
    }
    if (fields.length === 0) return setError("Add at least one field.");
    const seen = new Set<string>();
    const out: EntityField[] = [];
    for (const f of fields) {
      const fname = f.name.trim();
      if (!NAME_RE.test(fname)) return setError(`Field name “${fname || "(empty)"}” must be lowercase snake_case.`);
      if (seen.has(fname)) return setError(`Duplicate field “${fname}”.`);
      seen.add(fname);
      const field: EntityField = { name: fname, type: f.type, required: f.required };
      if (f.description.trim()) field.description = f.description.trim();
      if (f.weight !== undefined) field.weight = f.weight;
      if (f.type === "enum") {
        const choices = f.choices
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean);
        if (choices.length < 2) return setError(`Enum field “${fname}” needs at least two choices.`);
        field.choices = choices;
      }
      out.push(field);
    }
    setBusy(true);
    try {
      onSaved(await api.saveEntity(entity?.name ?? n, { description: description.trim(), fields: out }));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={entity ? `Edit ${entity.name}` : "New entity"}
      description={
        entity
          ? `Saving bumps the version (currently v${entity.version}). Learned layouts for this entity are re-learned as needed.`
          : "Describe what to extract. The AI uses the descriptions; learned layouts map each field."
      }
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="entity-form" variant="primary" loading={busy}>
            {entity ? "Save entity" : "Create entity"}
          </Button>
        </>
      }
    >
      <form id="entity-form" onSubmit={submit} className="space-y-5">
        {!entity && (
          <Field label="Name" hint="Used in the API, e.g. job_posting.">
            {(id, hint) => (
              <Input
                id={id}
                data-autofocus
                aria-describedby={hint}
                className="font-mono"
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase())}
              />
            )}
          </Field>
        )}
        <Field label="Description" hint="One sentence the AI reads to know what counts as this entity.">
          {(id, hint) => (
            <Textarea id={id} rows={2} aria-describedby={hint} value={description} onChange={(e) => setDescription(e.target.value)} />
          )}
        </Field>

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium">Fields</legend>
          {fields.map((f, i) => (
            <div key={f.key} className="space-y-3 rounded-lg border border-border p-3">
              <div className="grid gap-3 sm:grid-cols-[1fr_10rem]">
                <Field label="Field name">
                  {(id) => (
                    <Input
                      id={id}
                      className="font-mono"
                      value={f.name}
                      onChange={(e) => update(f.key, { name: e.target.value.toLowerCase() })}
                    />
                  )}
                </Field>
                <Field label="Type">
                  {(id) => (
                    <Select id={id} value={f.type} onChange={(e) => update(f.key, { type: e.target.value as FieldType })}>
                      {FIELD_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
              </div>
              <Field label="Description" optional>
                {(id) => <Input id={id} value={f.description} onChange={(e) => update(f.key, { description: e.target.value })} />}
              </Field>
              {f.type === "enum" && (
                <Field label="Choices" hint="Comma-separated.">
                  {(id, hint) => (
                    <Input
                      id={id}
                      aria-describedby={hint}
                      value={f.choices}
                      placeholder="full_time, part_time, contract"
                      onChange={(e) => update(f.key, { choices: e.target.value })}
                    />
                  )}
                </Field>
              )}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Checkbox label="Required" checked={f.required} onChange={(v) => update(f.key, { required: v })} />
                <div className="flex gap-1">
                  <Button size="sm" variant="ghost" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${f.name || "field"} up`}>
                    ↑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={i === fields.length - 1}
                    onClick={() => move(i, 1)}
                    aria-label={`Move ${f.name || "field"} down`}
                  >
                    ↓
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="hover:text-danger"
                    onClick={() => setFields((fs) => fs.filter((x) => x.key !== f.key))}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            </div>
          ))}
          <Button size="sm" onClick={() => setFields((fs) => [...fs, blankField()])}>
            Add field
          </Button>
        </fieldset>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}

export default function EntitiesPage() {
  const { canAdmin } = useSession();
  const confirm = useConfirm();
  const entities = useAsync(() => api.entities(), "entities");
  const [editing, setEditing] = useState<EntitySpec | null | "new">(null);
  const [open, setOpen] = useState<string | null>(null);

  async function remove(e: EntitySpec) {
    const ok = await confirm({
      title: `Delete entity “${e.name}”?`,
      message: "Sources and API calls that use it will fail. Stored items and learned layouts for it may be removed.",
      confirmLabel: "Delete entity",
      action: () => api.deleteEntity(e.name),
    });
    if (ok) entities.setData((prev) => (prev ?? []).filter((x) => x.name !== e.name));
  }

  return (
    <>
      <PageHeader
        title="Entities"
        description="The kinds of things scraper0 extracts. Built-in entities are maintained for you; custom ones are yours to define."
        actions={
          <AdminOnly>
            <Button variant="primary" onClick={() => setEditing("new")}>
              New entity
            </Button>
          </AdminOnly>
        }
      />
      <DataView state={entities} empty={<Card><EmptyState title="No entities available" /></Card>}>
        {(list) => {
          const builtin = list.filter((e) => e.builtin);
          const custom = list.filter((e) => !e.builtin);
          const renderList = (items: EntitySpec[]) => (
            <ul className="divide-y divide-border">
              {items.map((e) => {
                const expanded = open === e.name;
                return (
                  <li key={e.name}>
                    <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-5">
                      <div className="min-w-0">
                        <p className="flex items-center gap-2">
                          <span className="font-mono text-sm font-medium">{e.name}</span>
                          <Badge>v{e.version}</Badge>
                          <span className="text-xs text-subtle">{e.fields.length} fields</span>
                        </p>
                        {e.description && <p className="mt-0.5 text-sm text-muted">{e.description}</p>}
                      </div>
                      <div className="flex gap-1.5">
                        <Button size="sm" variant="ghost" onClick={() => setOpen(expanded ? null : e.name)} aria-expanded={expanded}>
                          {expanded ? "Hide fields" : "Fields"}
                        </Button>
                        {!e.builtin && canAdmin && (
                          <>
                            <Button size="sm" onClick={() => setEditing(e)}>
                              Edit
                            </Button>
                            <Button size="sm" variant="ghost" className="hover:text-danger" onClick={() => remove(e)}>
                              Delete
                            </Button>
                          </>
                        )}
                      </div>
                    </div>
                    {expanded && (
                      <div className="border-t border-border">
                        <FieldsTable fields={e.fields} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          );
          return (
            <div className="space-y-6">
              <Card>
                <CardHeader title="Built-in" description="Read-only. Available to every tenant." />
                {builtin.length ? renderList(builtin) : <EmptyState title="No built-in entities" />}
              </Card>
              <Card>
                <CardHeader title="Custom" description="Defined by your workspace." />
                {custom.length ? (
                  renderList(custom)
                ) : (
                  <EmptyState
                    title="No custom entities yet"
                    description="Define job postings, products, listings — any shape of data — as a list of fields."
                    action={
                      canAdmin && (
                        <Button size="sm" variant="primary" onClick={() => setEditing("new")}>
                          New entity
                        </Button>
                      )
                    }
                  />
                )}
              </Card>
            </div>
          );
        }}
      </DataView>

      {editing && (
        <EntityEditor
          entity={editing === "new" ? null : editing}
          existing={(entities.data ?? []).map((e) => e.name)}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            entities.setData((prev) => {
              const list = prev ?? [];
              return list.some((x) => x.name === saved.name)
                ? list.map((x) => (x.name === saved.name ? saved : x))
                : [...list, saved];
            });
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
