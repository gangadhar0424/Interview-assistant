/**
 * Local diagnostics (M-5, D-70). Main process only; nothing here is sent anywhere, and the report
 * text is only ever placed on the clipboard by the user's own action (the renderer never receives it).
 *
 * What it keeps: a small bounded ring of events with a FIXED set of types and fields (a type name from
 * a closed list, a reason from a closed list, small integers), in memory only. It is memory-only on
 * purpose: the main process may write only the settings and token stores to disk, so the log is lost
 * on restart. Nothing can record text: unknown reasons become "other", numbers are clamped
 * integers, and the report is built from typed fields, never from free text.
 *
 * What it can never contain: transcripts, prompts, answers, screenshots, audio, API keys,
 * authorization headers, passwords, session secrets, account ids or email addresses, the backend
 * host name, or file paths. As a second line of defence the finished report is scrubbed for anything
 * shaped like a secret, an email address or a user-profile path.
 *
 * Nothing in this module writes log output anywhere.
 */

export const DIAG_EVENT_TYPES = [
  "app_started",
  "window_created",
  "window_ready",
  "renderer_gone",
  "renderer_gone_ignored",
  "renderer_unresponsive",
  "renderer_responsive",
  "child_process_gone",
  "recovery_reload_started",
  "recovery_succeeded",
  "recovery_failed",
  "recovery_suppressed",
  "recovery_prompt",
  "diagnostics_copied",
] as const;
export type DiagEventType = (typeof DIAG_EVENT_TYPES)[number];

export const DIAG_REASONS = [
  // Electron's render-process-gone reasons
  "crashed",
  "oom",
  "killed",
  "launch-failed",
  "integrity-failure",
  "clean-exit",
  "abnormal-exit",
  // why a crash was ignored
  "quitting",
  "window-gone",
  // why recovery failed or was suppressed
  "crashed-again",
  "load-failed",
  "load-timeout",
  "too-many-reloads",
  // user choices in the failure dialog
  "retry",
  "copy",
  "dismiss",
  // child process kinds
  "gpu",
  "utility",
  "other",
] as const;
export type DiagReason = (typeof DIAG_REASONS)[number];

export type DiagEvent = {
  /** ISO timestamp (wall clock: for the reader, not for measuring durations). */
  at: string;
  type: DiagEventType;
  reason?: DiagReason;
  /** A small number: an exit code, a load error code or an attempt count. */
  code?: number;
  mode?: "auto" | "manual";
};

export const MAX_LOG_EVENTS = 200;
export const MAX_REPORT_EVENTS = 40;
export const MAX_REPORT_CHARS = 16_000;
const MAX_FIELD_CHARS = 120;

const EVENT_SET: ReadonlySet<string> = new Set(DIAG_EVENT_TYPES);
const REASON_SET: ReadonlySet<string> = new Set(DIAG_REASONS);

/** Remove anything shaped like a secret, an email address or a user-profile path. */
export function redactText(text: string): string {
  return text
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[email]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [redacted]")
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/g, "[token]")
    .replace(/\b(sk|gsk|pk|rk)[-_][A-Za-z0-9_-]{8,}/g, "[key]")
    .replace(/\bAIza[0-9A-Za-z_-]{20,}/g, "[key]")
    .replace(/[A-Za-z]:\\Users\\[^\\\s"']+/gi, "C:\\Users\\[user]")
    .replace(/\/(?:Users|home)\/[^/\s"']+/g, "/[user]")
    .replace(/[A-Za-z0-9+/_=-]{40,}/g, "[redacted]");
}

function clampInt(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  // Windows exit codes are 32-bit (a crash is often 0xC0000005, which is -1073741819 as signed).
  return Math.max(-2_147_483_648, Math.min(2_147_483_647, Math.trunc(value)));
}

export type AppInfo = {
  version: string;
  electron: string;
  chrome: string;
  node: string;
  platform: string;
  arch: string;
  osRelease: string;
  uptimeSeconds: number;
};

export type ServicesInfo = {
  signedIn: boolean;
  audioState: "idle" | "arming" | "listening";
  captureBusy: boolean;
  window: { visible: boolean; focused: boolean; minimized: boolean };
  settings: {
    audioInputEnabled: boolean;
    screenshotInputEnabled: boolean;
    alwaysOnTop: boolean;
    expanded: boolean;
  };
  /** "local" for a loopback backend address, otherwise "remote"; the host name is never included. */
  backend: "local" | "remote";
  acknowledgementAccepted: boolean;
  /** Per shortcut action: only the status kind. */
  shortcuts: Record<string, string>;
};

export type RecoveryCounters = {
  state: string;
  crashes: number;
  autoReloads: number;
  manualReloads: number;
  failures: number;
};

export type PerfSummaryLike = {
  metrics: Record<string, { count: number; p50: number; p95: number; max: number }>;
};

export type DiagnosticsProviders = {
  app?: () => AppInfo;
  services?: () => ServicesInfo;
  recovery?: () => RecoveryCounters;
  perf?: () => PerfSummaryLike;
};

export class DiagnosticsLog {
  private events: DiagEvent[] = [];
  private providers: DiagnosticsProviders = {};

  constructor(private readonly now: () => number = () => Date.now()) {}

  /** Record an event. Anything outside the closed sets is dropped or reduced to "other". */
  record(type: DiagEventType, fields: { reason?: string; code?: number; mode?: "auto" | "manual" } = {}): void {
    if (!EVENT_SET.has(type)) return;
    const event: DiagEvent = { at: new Date(this.now()).toISOString(), type };
    if (fields.reason !== undefined) {
      event.reason = (REASON_SET.has(fields.reason) ? fields.reason : "other") as DiagReason;
    }
    const code = clampInt(fields.code);
    if (code !== undefined) event.code = code;
    if (fields.mode === "auto" || fields.mode === "manual") event.mode = fields.mode;
    this.events.push(event);
    if (this.events.length > MAX_LOG_EVENTS) this.events.shift();
  }

  /** Attach (or detach, with `undefined`) a source of live state. Providers return typed data only. */
  provide<K extends keyof DiagnosticsProviders>(name: K, provider: DiagnosticsProviders[K] | undefined): void {
    if (provider) this.providers[name] = provider;
    else delete this.providers[name];
  }

  recent(count = MAX_REPORT_EVENTS): DiagEvent[] {
    return this.events.slice(-count);
  }

  size(): number {
    return this.events.length;
  }

  /** The support report: bounded, typed, scrubbed. Never throws; a failing provider is skipped. */
  report(): { text: string; truncated: boolean } {
    const lines: string[] = ["Interview Assistant diagnostics", `generated: ${new Date(this.now()).toISOString()}`];
    const safe = <T,>(provider: (() => T) | undefined): T | null => {
      try {
        return provider ? provider() : null;
      } catch {
        return null;
      }
    };
    const app = safe(this.providers.app);
    if (app) {
      lines.push("", "[application]");
      lines.push(`version: ${field(app.version)}`);
      lines.push(`electron: ${field(app.electron)}  chrome: ${field(app.chrome)}  node: ${field(app.node)}`);
      lines.push(`os: ${field(app.platform)} ${field(app.arch)} ${field(app.osRelease)}`);
      lines.push(`uptime seconds: ${Math.max(0, Math.trunc(app.uptimeSeconds))}`);
    }
    const services = safe(this.providers.services);
    if (services) {
      lines.push("", "[services]");
      lines.push(`signed in: ${yesNo(services.signedIn)}`);
      lines.push(`audio: ${field(services.audioState)}  screenshot capture in progress: ${yesNo(services.captureBusy)}`);
      lines.push(
        `window: visible ${yesNo(services.window.visible)}, focused ${yesNo(services.window.focused)}, minimized ${yesNo(services.window.minimized)}`,
      );
      lines.push(
        `settings: audio input ${yesNo(services.settings.audioInputEnabled)}, screenshot input ${yesNo(services.settings.screenshotInputEnabled)}, always on top ${yesNo(services.settings.alwaysOnTop)}, expanded ${yesNo(services.settings.expanded)}`,
      );
      lines.push(`backend: ${services.backend}`);
      lines.push(`acceptable-use notice accepted: ${yesNo(services.acknowledgementAccepted)}`);
      lines.push(
        `shortcuts: ${Object.entries(services.shortcuts)
          .map(([action, kind]) => `${field(action)}=${field(kind)}`)
          .join(", ")}`,
      );
    }
    const recovery = safe(this.providers.recovery);
    if (recovery) {
      lines.push("", "[recovery]");
      lines.push(
        `state: ${field(recovery.state)}  renderer stops: ${recovery.crashes}  automatic reloads: ${recovery.autoReloads}  manual reloads: ${recovery.manualReloads}  failed recoveries: ${recovery.failures}`,
      );
    }
    const perf = safe(this.providers.perf);
    if (perf) {
      const names = Object.keys(perf.metrics).sort();
      if (names.length > 0) {
        lines.push("", "[performance, ms: count / p50 / p95 / max]");
        for (const name of names) {
          const m = perf.metrics[name] as { count: number; p50: number; p95: number; max: number };
          lines.push(`${field(name)}: ${m.count} / ${num(m.p50)} / ${num(m.p95)} / ${num(m.max)}`);
        }
      }
    }
    const head = lines.join("\n");
    const eventLines = this.recent().map((event) => {
      const parts = [event.at, event.type];
      if (event.reason) parts.push(`reason=${event.reason}`);
      if (event.code !== undefined) parts.push(`code=${event.code}`);
      if (event.mode) parts.push(`mode=${event.mode}`);
      return parts.join(" ");
    });
    const header = `\n\n[recent events, oldest first; this log is kept in memory only]\n`;
    let text = redactText(`${head}${header}${eventLines.join("\n") || "(none)"}\n`);
    let truncated = false;
    if (text.length > MAX_REPORT_CHARS) {
      truncated = true;
      // Keep the header and the NEWEST events: drop the oldest event lines until it fits.
      const kept = [...eventLines];
      while (kept.length > 0 && redactText(`${head}${header}${kept.join("\n")}\n`).length > MAX_REPORT_CHARS - 24) {
        kept.shift();
      }
      text = `${redactText(`${head}${header}${kept.join("\n")}\n`)}[truncated]\n`;
      if (text.length > MAX_REPORT_CHARS) text = `${text.slice(0, MAX_REPORT_CHARS - 14)}\n[truncated]\n`;
    }
    return { text, truncated };
  }
}

function field(value: unknown): string {
  return String(value ?? "").replace(/[\r\n]+/g, " ").slice(0, MAX_FIELD_CHARS);
}
function yesNo(value: boolean): string {
  return value ? "yes" : "no";
}
function num(value: number): string {
  return Number.isFinite(value) ? String(Math.round(value * 100) / 100) : "n/a";
}

/** The one shared diagnostics log for the main process. */
export const diagnostics = new DiagnosticsLog();
