"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { SessionWizard } from "@/components/SessionWizard";
import { useAuth } from "@/lib/auth";

// Protected page (same pattern as /home). The client-side guard is a UX convenience only: every
// API call the wizard makes is authenticated and user-scoped by the server.
export default function NewSessionPage() {
  const { state } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (state.status === "anonymous") router.replace("/login");
  }, [state.status, router]);

  if (state.status !== "authenticated") {
    return <main style={{ padding: "2rem" }}>Loading…</main>;
  }

  return (
    <main style={{ padding: "2rem", display: "grid", gap: "1.5rem", justifyItems: "start" }}>
      <nav aria-label="Breadcrumb">
        <Link href="/home">&larr; Back to dashboard</Link>
      </nav>
      <SessionWizard />
    </main>
  );
}
