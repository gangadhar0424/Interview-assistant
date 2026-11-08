"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { FilesPanel } from "@/components/FilesPanel";
import { SessionsPanel } from "@/components/SessionsPanel";
import { useAuth } from "@/lib/auth";

// Protected dashboard: Call Sessions (with "Create session") plus resume/document management.
// Note: this client-side guard is a UX convenience; real access control is enforced by the API.
export default function HomePage() {
  const { state, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (state.status === "anonymous") router.replace("/login");
  }, [state.status, router]);

  if (state.status !== "authenticated") {
    return <main style={{ padding: "2rem" }}>Loading…</main>;
  }

  async function onLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <main style={{ padding: "2rem", display: "grid", gap: "2rem", justifyItems: "start" }}>
      <header
        style={{
          display: "flex",
          gap: "1rem",
          alignItems: "center",
          justifyContent: "space-between",
          width: "100%",
          maxWidth: "40rem",
        }}
      >
        <div>
          <h1 style={{ margin: 0 }}>Dashboard</h1>
          <p style={{ margin: 0, color: "#666" }}>Signed in as {state.session.user.email}</p>
        </div>
        <button type="button" onClick={onLogout} style={{ padding: "0.5rem 1rem" }}>
          Log out
        </button>
      </header>

      <SessionsPanel />

      <div id="files" style={{ display: "grid", gap: "2rem", width: "100%", justifyItems: "start" }}>
        <FilesPanel kind="resumes" title="Resumes" extensions={[".pdf", ".docx"]} maxMb={10} />
        <FilesPanel
          kind="documents"
          title="Documents"
          extensions={[".pdf", ".docx", ".txt"]}
          maxMb={20}
        />
      </div>
    </main>
  );
}
