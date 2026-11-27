import { useState } from "react";
import type { SessionSummary } from "../../../shared/contracts";
import {
  formatActivity,
  isUsable,
  lastActivityIso,
  sessionName,
  sessionState,
  stateLabel,
} from "../../../shared/session-view";
import { useDesktopStore } from "../store";

/**
 * One row of the session list (M-9): open it, see its state and last activity, rename, complete or
 * reopen, export, delete. Delete and export confirmations are native dialogs shown by the main
 * process; nothing here records, captures or exports anything by itself.
 */
export function SessionCard({
  session,
  current,
  onOpen,
}: {
  session: SessionSummary;
  current: boolean;
  onOpen: () => void;
}) {
  const busyId = useDesktopStore((state) => state.sessionBusyId);
  const renameSession = useDesktopStore((state) => state.renameSession);
  const endSession = useDesktopStore((state) => state.endSession);
  const reopenSession = useDesktopStore((state) => state.reopenSession);
  const deleteSession = useDesktopStore((state) => state.deleteSession);
  const exportSession = useDesktopStore((state) => state.exportSession);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const state = sessionState(session);
  const working = busyId === session.id;
  const anyBusy = busyId !== null;
  const completed = state === "completed";
  const when = formatActivity(lastActivityIso(session));
  const questions = session.question_count;

  async function saveName() {
    if (await renameSession(session.id, name)) setRenaming(false);
  }

  return (
    <div
      className={`session-card ${current ? "session-card-selected" : ""}`}
      aria-current={current ? "true" : undefined}
      data-testid="session-card"
    >
      <button className="session-open" disabled={working} onClick={onOpen} type="button">
        <span className="session-card-top">
          <span className="session-monogram">{session.company_name.slice(0, 1).toUpperCase()}</span>
          <span className="session-badges">
            {current && <span className="current-pill">Current</span>}
            <span className={`status-pill state-${state}`}>
              <span className="status-dot" />
              {stateLabel(state)}
            </span>
          </span>
        </span>
        <strong>{sessionName(session)}</strong>
        <span className="session-company">{session.company_name}</span>
        <span className="session-meta">
          {session.resume.id === null ? `${session.resume.filename} (deleted)` : session.resume.filename}{" "}
          · {session.document_count} supporting documents
          {questions !== undefined && ` · ${questions} ${questions === 1 ? "question" : "questions"}`}
          {when && ` · ${when}`}
        </span>
        {state === "interrupted" && (
          <span className="session-hint">
            An answer was cut off. Open the session to see it in the history and ask again.
          </span>
        )}
      </button>

      {renaming ? (
        <form
          className="session-rename"
          onSubmit={(event) => {
            event.preventDefault();
            void saveName();
          }}
        >
          <label className="sr-only" htmlFor={`rename-${session.id}`}>
            Session name
          </label>
          <input
            autoFocus
            className="field-control"
            id={`rename-${session.id}`}
            maxLength={200}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === "Escape" && setRenaming(false)}
            value={name}
          />
          <button className="button button-primary" disabled={anyBusy} type="submit">
            Save
          </button>
          <button className="button" onClick={() => setRenaming(false)} type="button">
            Cancel
          </button>
        </form>
      ) : (
        <div className="session-actions-row">
          <button
            className="link-button"
            disabled={anyBusy}
            onClick={() => {
              setName(session.title ?? "");
              setRenaming(true);
            }}
            type="button"
          >
            Rename
          </button>
          {completed ? (
            <button
              className="link-button"
              disabled={anyBusy || session.status === "archived"}
              onClick={() => void reopenSession(session.id)}
              type="button"
            >
              Reopen
            </button>
          ) : (
            <button
              className="link-button"
              disabled={anyBusy}
              onClick={() => void endSession(session.id)}
              title={isUsable(session) ? undefined : "This session is not ready"}
              type="button"
            >
              Complete
            </button>
          )}
          <button
            className="link-button"
            disabled={anyBusy}
            onClick={() => void exportSession(session.id, "markdown")}
            type="button"
          >
            Export
          </button>
          <button
            className="link-button"
            disabled={anyBusy}
            onClick={() => void exportSession(session.id, "pdf")}
            title="Export as PDF"
            type="button"
          >
            PDF
          </button>
          <button
            className="link-button link-danger"
            disabled={anyBusy}
            onClick={() => void deleteSession(session.id)}
            type="button"
          >
            Delete
          </button>
          {working && <span className="session-working">Working…</span>}
        </div>
      )}
    </div>
  );
}
