import type { Metadata } from "next";
import { Suspense } from "react";
import { AuthCard } from "@/components/auth-card";
import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <AuthCard title="Sign in" description="Use the email and password from your invite.">
      <Suspense>
        <LoginForm />
      </Suspense>
    </AuthCard>
  );
}
