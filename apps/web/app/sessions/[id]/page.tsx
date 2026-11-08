"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";
import { SessionChat } from "@/components/SessionChat";
import { useAuth } from "@/lib/auth";
import { isUuid } from "@/lib/qa";

// Protected page (same pattern as /home and /sessions/new). The client-side guard is a UX
// convenience only: every API call made here is authenticated and user-scoped by the server.
export default function SessionPage() {
  const { state } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const id = typeof params.id === "string" ? params.id : "";

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
      {isUuid(id) ? (
        <SessionChat sessionId={id} />
      ) : (
        <p role="alert" style={{ color: "#b00020" }}>
          This session was not found.
        </p>
      )}
    </main>
  );
}
