"use client";

import { useState, type FormEvent } from "react";
import { api, ApiError, errorMessage } from "@/lib/api";
import { Alert, Button, Field, Input, Textarea } from "./ui";

export function AccessRequestForm() {
  const [form, setForm] = useState({ name: "", email: "", organisation: "", use_case: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!form.name.trim() || !form.email.trim()) {
      setError("Please enter your name and email.");
      return;
    }
    setBusy(true);
    try {
      await api.requestAccess({
        name: form.name.trim(),
        email: form.email.trim(),
        organisation: form.organisation.trim() || undefined,
        use_case: form.use_case.trim() || undefined,
      });
      setDone(true);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 429
          ? "You've sent several requests already. Please try again in an hour."
          : errorMessage(err),
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div role="status" className="rounded-lg border border-border bg-surface p-6 text-center">
        <p className="text-base font-semibold">Thanks — your request is in.</p>
        <p className="mt-2 text-sm text-muted">
          We review requests by hand. If it&apos;s a fit, you&apos;ll get an invite link at {form.email}.
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4 rounded-lg border border-border bg-surface p-5 shadow-sm sm:p-6" noValidate>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          {(id) => <Input id={id} required autoComplete="name" value={form.name} onChange={set("name")} />}
        </Field>
        <Field label="Work email">
          {(id) => (
            <Input id={id} type="email" required autoComplete="email" value={form.email} onChange={set("email")} />
          )}
        </Field>
      </div>
      <Field label="Organisation" optional>
        {(id) => <Input id={id} autoComplete="organization" value={form.organisation} onChange={set("organisation")} />}
      </Field>
      <Field label="What would you like to extract?" optional>
        {(id) => (
          <Textarea
            id={id}
            rows={3}
            placeholder="e.g. community events from 40 venue websites, refreshed daily"
            value={form.use_case}
            onChange={set("use_case")}
          />
        )}
      </Field>
      {error && <Alert>{error}</Alert>}
      <Button type="submit" variant="primary" loading={busy} className="w-full sm:w-auto">
        Request access
      </Button>
    </form>
  );
}
