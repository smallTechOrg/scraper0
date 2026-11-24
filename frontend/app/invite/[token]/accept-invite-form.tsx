"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { MIN_PASSWORD } from "@/components/change-password";
import { Alert, Button, Field, Input } from "@/components/ui";
import { api, ApiError, errorMessage } from "@/lib/api";


export function AcceptInviteForm({ token }: { token: string }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError("Please enter your name.");
    if (password.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters for your password.`);
    if (password !== confirm) return setError("The passwords don't match.");
    setBusy(true);
    try {
      await api.acceptInvite(token,name.trim(), password);
      router.replace("/app");
    } catch (err) {
      if (err instanceof ApiError && err.code === "invalid_invite") setInvalid(true);
      else setError(errorMessage(err));
      setBusy(false);
    }
  }

  if (invalid) {
    return (
      <div className="space-y-4">
        <Alert>This invite link is invalid, expired or has already been used.</Alert>
        <p className="text-sm text-muted">
          Ask whoever invited you for a new link, or{" "}
          <Link href="/login" className="font-medium text-accent hover:underline">
            sign in
          </Link>{" "}
          if you already set up your account.
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <Field label="Your name">
        {(id) => <Input id={id} autoComplete="name" autoFocus value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label="Password" hint={`At least ${MIN_PASSWORD} characters.`}>
        {(id, hint) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            aria-describedby={hint}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
      </Field>
      <Field label="Confirm password">
        {(id) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        )}
      </Field>
      {error && <Alert>{error}</Alert>}
      <Button type="submit" variant="primary" className="w-full" loading={busy}>
        Create account
      </Button>
    </form>
  );
}
