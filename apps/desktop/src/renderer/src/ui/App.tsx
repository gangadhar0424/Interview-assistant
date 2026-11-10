import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useDesktopStore } from "../store";

type Page = "home" | "assistant" | "history" | "settings";

const navigation: Array<{ id: Page; label: string; icon: string }> = [
  { id: "home", label: "Sessions", icon: "⌂" },
  { id: "assistant", label: "Assistant", icon: "✦" },
  { id: "history", label: "History", icon: "↻" },
  { id: "settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const initialized = useDesktopStore((state) => state.initialized);
  const user = useDesktopStore((state) => state.user);
  const initialize = useDesktopStore((state) => state.initialize);
  const handleStreamMessage = useDesktopStore((state) => state.handleStreamMessage);
  const expireSession = useDesktopStore((state) => state.expireSession);
  const checkBackend = useDesktopStore((state) => state.checkBackend);
  const checked = useRef(false);
  const [page, setPage] = useState<Page>("home");

  useEffect(() => {
    if (checked.current) return;
    checked.current = true;
    void initialize();
    const unsubscribeStream = window.desktop.onStreamMessage(handleStreamMessage);
    const unsubscribeExpired = window.desktop.onSessionExpired(expireSession);
    const interval = window.setInterval(() => void checkBackend(), 15_000);
    return () => {
      unsubscribeStream();
      unsubscribeExpired();
      window.clearInterval(interval);
    };
  }, [initialize, handleStreamMessage, expireSession, checkBackend]);

  if (!initialized) return <LoadingScreen />;
  if (!user) return <LoginScreen />;

  return (
    <div className="window-shell">
      <header className="titlebar">
        <div className="drag-region">
          <div className="brand-mark">IA</div>
          <span className="brand-name">Interview Assistant</span>
          <ConnectionIndicator />
        </div>
        <div className="window-actions no-drag">
          <button
            aria-label="Minimize"
            className="window-control"
            onClick={() => void window.desktop.app.minimize()}
            title="Minimize"
          >
            −
          </button>
          <button
            aria-label="Close to tray"
            className="window-control close-control"
            onClick={() => void window.desktop.app.closeToTray()}
            title="Close to tray"
          >
            ×
          </button>
        </div>
      </header>

      <div className="app-content">
        <aside className="sidebar">
          {navigation.map((item) => (
            <button
              aria-current={page === item.id ? "page" : undefined}
              className={`nav-item ${page === item.id ? "nav-item-active" : ""}`}
              key={item.id}
              onClick={() => setPage(item.id)}
              title={item.label}
            >
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-label">{item.label}</span>
            </button>
          ))}
          <div className="sidebar-spacer" />
          <div className="user-chip" title={user.email}>
            <span className="user-avatar">{user.email.slice(0, 1).toUpperCase()}</span>
            <span className="user-email">{user.email}</span>
          </div>
        </aside>

        <main className="main-panel">
          <AnimatePresence mode="wait">
            <motion.section
              animate={{ opacity: 1, y: 0 }}
              className="page"
              exit={{ opacity: 0, y: 5 }}
              initial={{ opacity: 0, y: 5 }}
              key={page}
              transition={{ duration: 0.16 }}
            >
              {page === "home" && <HomePage openAssistant={() => setPage("assistant")} />}
              {page === "assistant" && <AssistantPage />}
              {page === "history" && <HistoryPage />}
              {page === "settings" && <SettingsPage />}
            </motion.section>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}

function LoadingScreen() {
  return (
    <div className="loading-screen">
      <div className="brand-mark brand-mark-large">IA</div>
      <p>Connecting to your assistant…</p>
    </div>
  );
}

function ConnectionIndicator() {
  const connection = useDesktopStore((state) => state.connection);
  const checkBackend = useDesktopStore((state) => state.checkBackend);
  const label =
    connection === "connected"
      ? "Backend connected"
      : connection === "checking"
        ? "Checking backend"
        : "Backend unavailable";
  return (
    <button
      className="connection-indicator no-drag"
      onClick={() => void checkBackend()}
      title={`${label} — click to check`}
    >
      <span className={`connection-dot connection-${connection}`} />
      {label}
    </button>
  );
}

function PageHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="muted-copy">{description}</p>
      </div>
      {action}
    </div>
  );
}

function HomePage({ openAssistant }: { openAssistant: () => void }) {
  const sessions = useDesktopStore((state) => state.sessions);
  const selectedSessionId = useDesktopStore((state) => state.selectedSessionId);
  const selectSession = useDesktopStore((state) => state.selectSession);
  const refreshSessions = useDesktopStore((state) => state.refreshSessions);
  const busy = useDesktopStore((state) => state.busy);
  const error = useDesktopStore((state) => state.error);

  return (
    <div className="page-scroll">
      <PageHeading
        action={
          <button className="icon-button" disabled={busy} onClick={() => void refreshSessions()}>
            ↻ <span className="sr-only">Refresh sessions</span>
          </button>
        }
        description="Choose a prepared session to open your floating assistant."
        eyebrow="YOUR WORKSPACE"
        title="Welcome back"
      />
      {error && <ErrorBanner message={error} />}
      {sessions.length === 0 ? (
        <div className="empty-card">
          <div className="empty-orbit">✦</div>
          <h2>No sessions yet</h2>
          <p>
            Create an interview session in the web app first. Your prepared sessions will appear
            here automatically.
          </p>
          <div className="connection-note">
            <span className="connection-dot connection-connected" />
            This desktop app connects to your existing backend.
          </div>
        </div>
      ) : (
        <div className="session-list">
          <div className="section-title-row">
            <h2>Call sessions</h2>
            <span className="count-pill">{sessions.length}</span>
          </div>
          {sessions.map((session) => (
            <button
              className={`session-card ${selectedSessionId === session.id ? "session-card-selected" : ""}`}
              key={session.id}
              onClick={() => {
                void selectSession(session.id);
                openAssistant();
              }}
            >
              <div className="session-card-top">
                <span className="session-monogram">
                  {session.company_name.slice(0, 1).toUpperCase()}
                </span>
                <span className={`status-pill status-${session.status}`}>
                  <span className="status-dot" />
                  {humanize(session.status)}
                </span>
              </div>
              <strong>{session.title || session.company_name}</strong>
              <span className="session-company">{session.company_name}</span>
              <span className="session-meta">
                {session.resume.filename} · {session.document_count} supporting documents
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function AssistantPage() {
  const sessions = useDesktopStore((state) => state.sessions);
  const selectedSessionId = useDesktopStore((state) => state.selectedSessionId);
  const selected = sessions.find((session) => session.id === selectedSessionId);
  const selectSession = useDesktopStore((state) => state.selectSession);
  const liveAnswer = useDesktopStore((state) => state.liveAnswer);
  const ask = useDesktopStore((state) => state.ask);
  const cancel = useDesktopStore((state) => state.cancel);
  const reportError = useDesktopStore((state) => state.reportError);
  const error = useDesktopStore((state) => state.error);
  const [question, setQuestion] = useState("");
  const [copied, setCopied] = useState(false);
  const streaming =
    !!liveAnswer && ["sending", "retrieving", "generating", "streaming"].includes(liveAnswer.phase);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = question.trim();
    if (!text || streaming) return;
    setQuestion("");
    await ask(text);
  }

  async function copyAnswer() {
    if (!liveAnswer?.text) return;
    try {
      const result = await window.desktop.app.copyText(liveAnswer.text);
      if (!result.ok) {
        reportError(result.error.message);
        return;
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      reportError("The answer could not be copied.");
    }
  }

  return (
    <div className="page-scroll">
      <PageHeading
        description="Ask a question and receive a streamed answer grounded in the selected session."
        eyebrow="LIVE ASSISTANT"
        title="Ask anything"
      />
      {error && <ErrorBanner message={error} />}
      {sessions.length === 0 ? (
        <div className="empty-card compact-empty">
          <div className="empty-orbit">⌂</div>
          <h2>Choose a session first</h2>
          <p>Create a session in the web app, then return here to ask a question.</p>
        </div>
      ) : (
        <>
          <label className="field-label" htmlFor="active-session">Active session</label>
          <select
            className="field-control session-select"
            id="active-session"
            onChange={(event) => void selectSession(event.target.value)}
            value={selectedSessionId ?? ""}
          >
            {sessions.map((session) => (
              <option key={session.id} value={session.id}>
                {session.title || session.company_name} · {session.company_name}
              </option>
            ))}
          </select>

          <div className="active-session-card">
            <span className={`status-pill status-${selected?.status ?? "unknown"}`}>
              <span className="status-dot" />
              {humanize(selected?.status ?? "unknown")}
            </span>
            <span>{selected?.resume.filename ?? "Resume unavailable"}</span>
          </div>

          <form className="question-form" onSubmit={(event) => void submit(event)}>
            <label className="field-label" htmlFor="question">Your question</label>
            <textarea
              autoComplete="off"
              className="field-control question-input"
              disabled={streaming || selected?.status !== "ready"}
              id="question"
              maxLength={2000}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={
                selected?.status === "ready"
                  ? "e.g. How would you describe your experience leading a team?"
                  : "This session is not ready for questions yet."
              }
              rows={4}
              value={question}
            />
            <div className="composer-footer">
              <span className="character-count">{question.length}/2000</span>
              {streaming ? (
                <button className="button button-stop" onClick={() => void cancel()} type="button">
                  Stop generation
                </button>
              ) : (
                <button
                  className="button button-primary"
                  disabled={!question.trim() || selected?.status !== "ready"}
                  type="submit"
                >
                  <span>Ask</span>
                  <span aria-hidden="true">↗</span>
                </button>
              )}
            </div>
          </form>

          {liveAnswer && <LiveAnswerCard answer={liveAnswer} onCopy={() => void copyAnswer()} copied={copied} />}
        </>
      )}
    </div>
  );
}

function LiveAnswerCard({
  answer,
  onCopy,
  copied,
}: {
  answer: NonNullable<ReturnType<typeof useDesktopStore.getState>["liveAnswer"]>;
  onCopy: () => void;
  copied: boolean;
}) {
  const phaseLabel: Record<string, string> = {
    sending: "Sending question",
    retrieving: "Searching selected materials",
    generating: "Preparing answer",
    streaming: "Writing answer",
    done: "Complete",
    failed: "Unable to answer",
    cancelled: "Stopped",
  };
  return (
    <article aria-live="polite" className="answer-card">
      <div className="answer-header">
        <div>
          <span className="eyebrow">YOUR QUESTION</span>
          <p className="question-quote">{answer.question}</p>
        </div>
        <span className={`answer-phase answer-phase-${answer.phase}`}>
          {["sending", "retrieving", "generating", "streaming"].includes(answer.phase) && (
            <span className="pulse-dot" />
          )}
          {phaseLabel[answer.phase]}
        </span>
      </div>
      <div className="answer-body">
        {answer.text ? (
          <p className="answer-text">{answer.text}</p>
        ) : (
          <p className="answer-placeholder">
            {answer.errorMessage ?? "Your answer will appear here as it is generated."}
          </p>
        )}
      </div>
      {answer.errorMessage && answer.text && (
        <p className="inline-error">{answer.errorMessage}</p>
      )}
      <div className="answer-footer">
        <span>
          {answer.provider && answer.model
            ? `${answer.provider} · ${answer.model}`
            : answer.phase === "done"
              ? "Answer ready"
              : "Session context only"}
          {answer.metrics?.total_ms != null ? ` · ${(answer.metrics.total_ms / 1000).toFixed(1)}s` : ""}
        </span>
        <button
          className="text-button"
          disabled={!answer.text}
          onClick={onCopy}
          type="button"
        >
          {copied ? "Copied" : "Copy answer"}
        </button>
      </div>
    </article>
  );
}

function HistoryPage() {
  const history = useDesktopStore((state) => state.history);
  const sessions = useDesktopStore((state) => state.sessions);
  const selectedSessionId = useDesktopStore((state) => state.selectedSessionId);
  const selectSession = useDesktopStore((state) => state.selectSession);
  const loadHistory = useDesktopStore((state) => state.loadHistory);
  const session = sessions.find((item) => item.id === selectedSessionId);

  return (
    <div className="page-scroll">
      <PageHeading
        action={
          <button className="icon-button" onClick={() => void loadHistory()} title="Refresh history">
            ↻
          </button>
        }
        description="Previously asked questions and answers for this session."
        eyebrow="RESPONSE HISTORY"
        title="Your answers"
      />
      {sessions.length > 1 && (
        <select
          aria-label="Choose session history"
          className="field-control session-select history-select"
          onChange={(event) => void selectSession(event.target.value)}
          value={selectedSessionId ?? ""}
        >
          {sessions.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title || item.company_name}
            </option>
          ))}
        </select>
      )}
      <p className="history-session-name">{session?.company_name ?? "No session selected"}</p>
      {history.length === 0 ? (
        <div className="empty-card compact-empty">
          <div className="empty-orbit">↻</div>
          <h2>No responses yet</h2>
          <p>Answers from this session will appear here.</p>
        </div>
      ) : (
        <div className="history-list">
          {[...history].reverse().map((item) => (
            <article className="history-card" key={item.id}>
              <div className="history-question">
                <span className="history-index">Q</span>
                <div>
                  <p>{item.text}</p>
                  <time>{formatDate(item.created_at)}</time>
                </div>
              </div>
              {item.answers.length === 0 ? (
                <p className="history-answer-muted">No saved answer.</p>
              ) : (
                item.answers.map((answer) => (
                  <div className="history-answer" key={answer.id}>
                    <p className="answer-text">{answer.text || answerErrorLabel(answer.error_code)}</p>
                    <div className="history-answer-meta">
                      <span className={`mini-status mini-status-${answer.status}`}>
                        {humanize(answer.status)}
                      </span>
                      <span>{answer.provider && answer.model ? `${answer.provider} · ${answer.model}` : "—"}</span>
                      <span>{answer.total_ms == null ? "—" : `${(answer.total_ms / 1000).toFixed(1)}s`}</span>
                    </div>
                  </div>
                ))
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function SettingsPage() {
  const settings = useDesktopStore((state) => state.settings);
  const updateSettings = useDesktopStore((state) => state.updateSettings);
  const checkBackend = useDesktopStore((state) => state.checkBackend);
  const logout = useDesktopStore((state) => state.logout);
  const connection = useDesktopStore((state) => state.connection);
  const error = useDesktopStore((state) => state.error);
  const [backendUrl, setBackendUrl] = useState(settings?.backendBaseUrl ?? "");
  const [saved, setSaved] = useState(false);

  useEffect(() => setBackendUrl(settings?.backendBaseUrl ?? ""), [settings?.backendBaseUrl]);

  if (!settings) return null;

  async function saveBackend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const updated = await updateSettings({ backendBaseUrl: backendUrl });
    if (!updated) return;
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1800);
    await checkBackend();
  }

  return (
    <div className="page-scroll">
      <PageHeading
        description="Connection, window, and account preferences."
        eyebrow="PREFERENCES"
        title="Settings"
      />
      {error && <ErrorBanner message={error} />}

      <section className="settings-section">
        <h2>Backend connection</h2>
        <p className="settings-help">Use HTTPS outside local development.</p>
        <form className="settings-form" onSubmit={(event) => void saveBackend(event)}>
          <label className="field-label" htmlFor="backend-url">Backend URL</label>
          <input
            autoComplete="url"
            className="field-control"
            id="backend-url"
            onChange={(event) => setBackendUrl(event.target.value)}
            placeholder="https://api.example.com"
            value={backendUrl}
          />
          <div className="settings-action-row">
            <button className="button button-secondary" type="submit">Save URL</button>
            <span className="saved-hint">{saved ? "Saved" : ""}</span>
          </div>
        </form>
        <div className="connection-row">
          <span className={`connection-dot connection-${connection}`} />
          <span>{connection === "connected" ? "Connected" : connection === "checking" ? "Checking…" : "Unavailable"}</span>
          <button className="text-button" onClick={() => void checkBackend()} type="button">
            Test connection
          </button>
        </div>
      </section>

      <section className="settings-section">
        <h2>Floating window</h2>
        <ToggleRow
          checked={settings.alwaysOnTop}
          label="Keep on top"
          onChange={(alwaysOnTop) => void updateSettings({ alwaysOnTop })}
          description="Keep the visible assistant above other windows."
        />
        <div className="opacity-setting">
          <div className="setting-label-row">
            <span>Window opacity</span>
            <span className="opacity-value">{Math.round(settings.opacity * 100)}%</span>
          </div>
          <input
            aria-label="Window opacity"
            className="opacity-slider"
            max="1"
            min="0.65"
            onChange={(event) => void updateSettings({ opacity: Number(event.target.value) })}
            step="0.01"
            type="range"
            value={settings.opacity}
          />
          <p className="settings-help">Opacity stays at or above 65% so the window remains visible.</p>
        </div>
        <button
          className="button button-secondary full-button"
          onClick={() => void updateSettings({ expanded: !settings.expanded })}
          type="button"
        >
          {settings.expanded ? "Use compact layout" : "Use expanded layout"}
        </button>
      </section>

      <section className="settings-section settings-account">
        <h2>Account</h2>
        <p className="settings-help">Sign out removes the saved refresh credential from this device.</p>
        <button className="button button-secondary full-button" onClick={() => void logout()} type="button">
          Sign out
        </button>
      </section>
      <p className="privacy-footnote">
        Questions are sent only when you submit them. This app does not capture audio or screenshots.
      </p>
    </div>
  );
}

function ToggleRow({
  checked,
  label,
  description,
  onChange,
}: {
  checked: boolean;
  label: string;
  description: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="toggle-row">
      <span>
        <span className="toggle-label">{label}</span>
        <span className="settings-help">{description}</span>
      </span>
      <input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
    </label>
  );
}

function LoginScreen() {
  const login = useDesktopStore((state) => state.login);
  const signup = useDesktopStore((state) => state.signup);
  const error = useDesktopStore((state) => state.error);
  const clearError = useDesktopStore((state) => state.clearError);
  const busy = useDesktopStore((state) => state.busy);
  const connection = useDesktopStore((state) => state.connection);
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    clearError();
    try {
      if (mode === "signup") await signup(email, password);
      else await login(email, password);
    } catch {
      // The store provides the safe error message for this screen.
    }
  }

  return (
    <div className="login-shell">
      <header className="titlebar">
        <div className="drag-region">
          <div className="brand-mark">IA</div>
          <span className="brand-name">Interview Assistant</span>
          <ConnectionIndicator />
        </div>
        <div className="window-actions no-drag">
          <button aria-label="Minimize" className="window-control" onClick={() => void window.desktop.app.minimize()}>−</button>
          <button aria-label="Close to tray" className="window-control close-control" onClick={() => void window.desktop.app.closeToTray()}>×</button>
        </div>
      </header>
      <div className="login-content">
        <div className="login-hero">
          <div className="hero-glow" />
          <div className="brand-mark brand-mark-large">IA</div>
          <p className="eyebrow">YOUR INTERVIEW COMPANION</p>
          <h1>Show up ready.</h1>
          <p className="login-subtitle">A focused desktop space for your prepared sessions and answers.</p>
        </div>
        <div className="login-card">
          <div className="auth-tabs" role="tablist">
            <button
              aria-selected={mode === "login"}
              className={mode === "login" ? "auth-tab auth-tab-active" : "auth-tab"}
              onClick={() => { setMode("login"); clearError(); }}
              role="tab"
              type="button"
            >
              Sign in
            </button>
            <button
              aria-selected={mode === "signup"}
              className={mode === "signup" ? "auth-tab auth-tab-active" : "auth-tab"}
              onClick={() => { setMode("signup"); clearError(); }}
              role="tab"
              type="button"
            >
              Create account
            </button>
          </div>
          <form className="login-form" onSubmit={(event) => void submit(event)}>
            <label className="field-label" htmlFor="email">Email</label>
            <input
              autoComplete="username"
              className="field-control"
              id="email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
            <label className="field-label" htmlFor="password">Password</label>
            <input
              autoComplete={mode === "signup" ? "new-password" : "current-password"}
              className="field-control"
              id="password"
              minLength={mode === "signup" ? 10 : undefined}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
            {error && <ErrorBanner message={error} />}
            <button
              className="button button-primary full-button"
              disabled={busy || connection !== "connected"}
              type="submit"
            >
              {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
              {!busy && <span aria-hidden="true">↗</span>}
            </button>
            {connection !== "connected" && (
              <p className="login-hint">Connect to the backend before signing in.</p>
            )}
          </form>
          <p className="secure-note">
            <span>●</span> Sign-in credentials stay in the protected desktop process.
          </p>
        </div>
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="error-banner" role="alert">
      <span aria-hidden="true">!</span>
      <p>{message}</p>
    </div>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function answerErrorLabel(code: string | null): string {
  if (!code) return "No answer text was saved.";
  return `Answer ended with ${humanize(code)}.`;
}
