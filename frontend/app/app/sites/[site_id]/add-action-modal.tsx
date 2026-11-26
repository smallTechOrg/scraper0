"use client";

import { useState, type FormEvent } from "react";
import { Modal } from "@/components/modal";
import { normaliseUrl } from "@/components/sites";
import { Alert, Button, Checkbox, Field, Input, Select, Textarea } from "@/components/ui";
import { api, errorMessage, sitesApi, type Action, type Site } from "@/lib/api";
import { useAsync } from "@/lib/hooks";

const NAME_RE = /^[a-z][a-z0-9_]{0,62}$/;

/** Add a read action (writes are code-only and can't be added here). */
export function AddActionModal({
  site,
  open,
  onClose,
  onCreated,
}: {
  site: Site;
  open: boolean;
  onClose: () => void;
  onCreated: (action: Action, learnNow: boolean) => void;
}) {
  const entities = useAsync(() => api.entities(), "entities");
  const [name, setName] = useState("fetch_events");
  const [entity, setEntity] = useState("event");
  const [seeds, setSeeds] = useState(site.base_url);
  const [learnNow, setLearnNow] = useState(true);
  const [errors, setErrors] = useState<{ name?: string; seeds?: string }>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const entityList = entities.data ?? [];
  const entityKnown = entityList.some((e) => e.name === entity);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: typeof errors = {};
    const n = name.trim();
    if (!NAME_RE.test(n)) next.name = "Use lowercase letters, digits and underscores, starting with a letter (e.g. fetch_events).";
    const lines = seeds
      .split(/\s*\n\s*/)
      .map((l) => l.trim())
      .filter(Boolean);
    const urls = lines.map(normaliseUrl);
    const bad = lines.filter((_, i) => !urls[i]);
    if (bad.length) next.seeds = `Not a valid URL: ${bad.slice(0, 2).join(", ")}${bad.length > 2 ? "…" : ""}`;
    setErrors(next);
    if (next.name || next.seeds) return;

    setBusy(true);
    setError(null);
    try {
      const created = await sitesApi.createAction(site.site_id, {
        name: n,
        kind: "read",
        entity,
        // Omitted → the API defaults to [base_url].
        ...(urls.length ? { seed_urls: urls as string[] } : {}),
      });
      setBusy(false);
      onCreated(created, learnNow);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => !busy && onClose()}
      title="Add an action"
      description={
        <>
          A read action that scraper0 learns from <span className="font-mono">{site.base_url}</span>. Write actions are
          code connectors and can&apos;t be added here.
        </>
      }
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="add-action" loading={busy}>
            {learnNow ? "Add and learn" : "Add action"}
          </Button>
        </>
      }
    >
      <form id="add-action" onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Name" error={errors.name} hint="Callers use this name in the endpoint.">
          {(id, describedBy) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              aria-invalid={!!errors.name || undefined}
              className="font-mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
              data-autofocus
            />
          )}
        </Field>
        <Field
          label="Entity"
          hint={
            entities.error
              ? `Couldn't load entities (${errorMessage(entities.error)}).`
              : "What each extracted item is. Define custom entities on the Entities page."
          }
        >
          {(id, describedBy) => (
            <Select
              id={id}
              aria-describedby={describedBy}
              value={entity}
              onChange={(e) => setEntity(e.target.value)}
              disabled={entities.loading && !entities.data}
            >
              {!entityKnown && <option value={entity}>{entity}</option>}
              {entityList.map((e) => (
                <option key={e.name} value={e.name}>
                  {e.name}
                  {e.description ? ` — ${e.description}` : ""}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Seed URLs" error={errors.seeds} hint="One per line: the listing pages to learn from. Defaults to the base URL.">
          {(id, describedBy) => (
            <Textarea
              id={id}
              aria-describedby={describedBy}
              aria-invalid={!!errors.seeds || undefined}
              rows={4}
              className="font-mono text-xs"
              value={seeds}
              onChange={(e) => setSeeds(e.target.value)}
            />
          )}
        </Field>
        <Checkbox
          label="Start learning right away"
          hint="Renders the pages, finds structured data or the JSON API, and shows the items as a draft version."
          checked={learnNow}
          onChange={setLearnNow}
        />
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}
