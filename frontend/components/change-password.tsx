"use client";

import { useState, type FormEvent } from "react";
import { api, ApiError, errorMessage } from "@/lib/api";
import { Modal } from "./modal";
import { Alert, Button, Field, Input } from "./ui";

export const MIN_PASSWORD = 10;

export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!current) return setError("Enter your current password.");
    if (next.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters for the new password.`);
    if (next !== confirm) return setError("The new passwords don't match.");
    if (next === current) return setError("The new password must be different from the current one.");
    setBusy(true);
    try {
      await api.changePassword(current, next);
      setDone(true);
    } catch (err) {
      setError(
        err instanceof ApiError && (err.code === "invalid_credentials" || err.status === 401)
          ? "Your current password is incorrect."
          : errorMessage(err),
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <Modal
        open
        onClose={onClose}
        title="Password changed"
        footer={
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        <p className="text-sm text-muted">Your password was updated. You&apos;ve been signed out on other devices.</p>
      </Modal>
    );
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Change password"
      description="Changing your password signs you out everywhere else."
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="password-form" variant="primary" loading={busy}>
            Change password
          </Button>
        </>
      }
    >
      <form id="password-form" onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Current password">
          {(id) => (
            <Input
              id={id}
              data-autofocus
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          )}
        </Field>
        <Field label="New password" hint={`At least ${MIN_PASSWORD} characters.`}>
          {(id, hint) => (
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              aria-describedby={hint}
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          )}
        </Field>
        <Field label="Confirm new password">
          {(id) => (
            <Input id={id} type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          )}
        </Field>
        {error && <Alert>{error}</Alert>}
      </form>
    </Modal>
  );
}
