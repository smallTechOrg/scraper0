import type { Metadata } from "next";
import { AuthCard } from "@/components/auth-card";
import { AcceptInviteForm } from "./accept-invite-form";

export const metadata: Metadata = { title: "Accept invite" };

export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return (
    <AuthCard title="Join scraper0" description="Choose your name and a password to finish setting up your account.">
      <AcceptInviteForm token={token} />
    </AuthCard>
  );
}
