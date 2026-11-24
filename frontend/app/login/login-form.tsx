"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { safeNext } from "@/components/auth-card";
import { Alert, Button, Field, Input } from "@/components/ui";
import { api, ApiError, errorMessage } from "@/lib/api";

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(email.trim(), password);
      router.replace(safeNext(params.get("next")));
    } catch (err) {
      setError(
        err instanceof ApiError && (err.code === "invalid_credentials" || err.status === 401)
          ? "That email and password don't match."
          : errorMessage(err),
      );
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <Field label="Email">
        {(id) => (
          <Input
            id={id}
            type="email"
            autoComplete="email"
            required
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        )}
      </Field>
      <Field label="Password">
        {(id) => (
          <Input
            id={id}
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
      </Field>
      {error && <Alert>{error}</Alert>}
      <Button type="submit" variant="primary" className="w-full" loading={busy}>
        Sign in
      </Button>
      <p className="text-center text-sm text-muted">
        No account?{" "}
        <Link href="/#access" className="font-medium text-accent hover:underline">
          Request access
        </Link>
      </p>
    </form>
  );
}
