"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth";
import {
  browserWatchForLeave,
  launchDesktop,
  parseDownloadUrl,
  type HandoffIssue,
} from "@/lib/handoff";

type Phase = "idle" | "starting" | "opened" | "not-opened" | "error";

// Inlined at build time. Not hard-coded: the installer location is deployment configuration.
const DOWNLOAD_URL = parseDownloadUrl(process.env.NEXT_PUBLIC_DESKTOP_DOWNLOAD_URL);

// "Start Interview": opens the installed desktop app on this prepared session. The link carries
// only a short-lived, single-use code (never session data or credentials), and the code is never
// displayed. Starting the desktop app does not start any recording: that stays a separate,
// explicit step inside the desktop app.
export function DesktopHandoff({ sessionId, compact = false }: { sessionId: string; compact?: boolean }) {
  const { authedRequest } = useAuth();
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const start = useCallback(async () => {
    setPhase("starting");
    setMessage("");
    const outcome = await launchDesktop({
      requestCode: () =>
        authedRequest<HandoffIssue>(`/api/v1/sessions/${sessionId}/handoff`, { method: "POST" }),
      navigate: (url) => window.location.assign(url),
      watchForLeave: browserWatchForLeave,
    });
    if (!mountedRef.current) return;
    if (outcome.kind === "error") setMessage(outcome.message);
    setPhase(outcome.kind);
  }, [authedRequest, sessionId]);

  const busy = phase === "starting";

  return (
    <section
      aria-labelledby={compact ? undefined : "handoff-title"}
      className={compact ? "handoff-compact" : undefined}
      style={{ display: "grid", gap: "0.5rem", justifyItems: "start" }}
    >
      {!compact && (
        <h2 id="handoff-title" style={{ margin: 0, fontSize: "1.1rem" }}>
          Interview Assistant desktop
        </h2>
      )}
      <button type="button" className="start-interview" onClick={() => void start()} disabled={busy}>
        {busy ? "Opening…" : "Start Interview"}
      </button>
      {!compact && (
        <p className="handoff-install" style={{ margin: 0, fontSize: "0.88rem", color: "#64748b" }}>
          Needs the Interview Assistant desktop app.{" "}
          {DOWNLOAD_URL ? (
            <a href={DOWNLOAD_URL} rel="noopener noreferrer">
              Download the Windows installer
            </a>
          ) : (
            "Ask your administrator for the installer."
          )}
        </p>
      )}

      {phase === "opened" && (
        <p role="status" style={{ margin: 0 }}>
          Opening the desktop app. It will show this session as ready. Nothing is recorded until you
          start it there.
        </p>
      )}

      {phase === "error" && (
        <p role="alert" style={{ margin: 0, color: "#b00020" }}>
          {message}
        </p>
      )}

      {phase === "not-opened" && (
        <div role="alert" data-testid="handoff-fallback" style={{ display: "grid", gap: "0.4rem" }}>
          <p style={{ margin: 0 }}>
            The desktop app did not open. It may not be installed, or your browser may have blocked
            the request.
          </p>
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            <li>
              If it is installed, allow your browser to open it and choose Start Interview again.
            </li>
            <li>
              If it is not installed,{" "}
              {DOWNLOAD_URL ? (
                <a href={DOWNLOAD_URL} rel="noopener noreferrer">
                  download and install the desktop app
                </a>
              ) : (
                "ask your administrator for the desktop installer"
              )}
              , sign in there, then choose Start Interview again.
            </li>
          </ul>
        </div>
      )}
    </section>
  );
}
