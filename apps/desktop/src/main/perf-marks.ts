/**
 * Performance marks (M-2). Main process only; nothing here is reachable from the renderer or the
 * preload, and there is no IPC channel for it.
 *
 * What it records: monotonic timestamps for a FIXED set of stage names, a few numeric values, and
 * an outcome, grouped into traces identified by a random UUID. What it cannot record: any text. Stage
 * and value names are a closed set checked at runtime, ids must be UUIDs, and values must be finite
 * non-negative numbers, so audio, transcripts, questions, answers, screenshots and credentials have
 * no way in even if a caller passes the wrong thing.
 *
 * Time source: `performance.now()` (monotonic, unaffected by wall-clock changes). It is injected so
 * tests are exact. Everything is held in memory with fixed caps; a report file is written only when
 * the user starts the app with `INTERVIEW_PERF=1` (see index.ts).
 */
import { randomUUID } from "node:crypto";

export const PERF_STAGES = [
  // startup
  "module_loaded",
  "app_ready",
  "window_created",
  "ready_to_show",
  "did_finish_load",
  // audio arming
  "arm_requested",
  "listening",
  // audio segment -> transcript
  "segment_created",
  "request_start",
  "response",
  "utterance_emitted",
  // screenshot
  "capture_requested",
  "captured",
  "upload_start",
  "extracted",
  // answer
  "submitted",
  "first_event",
  "retrieval_done",
  "first_token",
  "done",
] as const;
export type PerfStage = (typeof PERF_STAGES)[number];

export const PERF_VALUES = ["server_ms", "retrieval_ms", "audio_ms", "bytes"] as const;
export type PerfValue = (typeof PERF_VALUES)[number];

export const PERF_KINDS = ["startup", "audio_arm", "audio", "screenshot", "answer"] as const;
export type PerfKind = (typeof PERF_KINDS)[number];

export type PerfOutcome = "ok" | "error" | "cancelled";

const STAGE_SET: ReadonlySet<string> = new Set(PERF_STAGES);
const VALUE_SET: ReadonlySet<string> = new Set(PERF_VALUES);
const KIND_SET: ReadonlySet<string> = new Set(PERF_KINDS);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const MAX_ACTIVE_TRACES = 64;
export const MAX_COMPLETED_TRACES = 500;
export const MAX_ALIASES = 200;

type Trace = {
  id: string;
  kind: PerfKind;
  parent: string | null;
  marks: Map<PerfStage, number>;
  values: Map<PerfValue, number>;
  outcome: PerfOutcome | null;
};

export type PerfCounters = {
  duplicateIds: number;
  droppedMarks: number;
  evictedActive: number;
};

/** One reported latency: how it is measured and what it covers. */
type MetricSpec = {
  name: string;
  kind: PerfKind;
  from: PerfStage;
  to: PerfStage;
  /** Where the time is spent, so local, network and provider time are never mixed up. */
  scope: "local" | "client_to_server_round_trip" | "end_to_end";
};

export const METRICS: readonly MetricSpec[] = [
  { name: "startup.module_to_app_ready_ms", kind: "startup", from: "module_loaded", to: "app_ready", scope: "local" },
  { name: "startup.app_ready_to_window_created_ms", kind: "startup", from: "app_ready", to: "window_created", scope: "local" },
  { name: "startup.window_created_to_ready_to_show_ms", kind: "startup", from: "window_created", to: "ready_to_show", scope: "local" },
  { name: "startup.window_created_to_did_finish_load_ms", kind: "startup", from: "window_created", to: "did_finish_load", scope: "local" },
  { name: "startup.module_to_did_finish_load_ms", kind: "startup", from: "module_loaded", to: "did_finish_load", scope: "local" },
  { name: "startup.module_to_ready_to_show_ms", kind: "startup", from: "module_loaded", to: "ready_to_show", scope: "local" },
  { name: "audio.arm_to_listening_ms", kind: "audio_arm", from: "arm_requested", to: "listening", scope: "local" },
  { name: "audio.segment_queue_wait_ms", kind: "audio", from: "segment_created", to: "request_start", scope: "local" },
  { name: "audio.transcribe_request_ms", kind: "audio", from: "request_start", to: "response", scope: "client_to_server_round_trip" },
  { name: "audio.segment_to_utterance_ms", kind: "audio", from: "segment_created", to: "utterance_emitted", scope: "end_to_end" },
  { name: "screenshot.acquisition_ms", kind: "screenshot", from: "capture_requested", to: "captured", scope: "local" },
  { name: "screenshot.extract_request_ms", kind: "screenshot", from: "upload_start", to: "extracted", scope: "client_to_server_round_trip" },
  { name: "answer.submit_to_first_event_ms", kind: "answer", from: "submitted", to: "first_event", scope: "client_to_server_round_trip" },
  { name: "answer.submit_to_first_token_ms", kind: "answer", from: "submitted", to: "first_token", scope: "end_to_end" },
  { name: "answer.submit_to_done_ms", kind: "answer", from: "submitted", to: "done", scope: "end_to_end" },
  { name: "answer.first_token_to_done_ms", kind: "answer", from: "first_token", to: "done", scope: "end_to_end" },
];

export type Stats = { count: number; min: number; p50: number; p95: number; max: number };

/** Nearest-rank percentile of an ascending sorted, non-empty array. */
export function percentile(sorted: readonly number[], p: number): number {
  const rank = Math.ceil((p / 100) * sorted.length);
  return sorted[Math.min(sorted.length - 1, Math.max(0, rank - 1))] as number;
}

export function stats(values: readonly number[]): Stats | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return {
    count: sorted.length,
    min: sorted[0] as number,
    p50: percentile(sorted, 50),
    p95: percentile(sorted, 95),
    max: sorted[sorted.length - 1] as number,
  };
}

export type PerfSink = {
  begin(kind: PerfKind, id: string, parent?: string): boolean;
  mark(id: string, stage: PerfStage): void;
  value(id: string, name: PerfValue, amount: number): void;
  alias(alias: string, id: string): void;
  end(id: string, outcome: PerfOutcome): void;
};

export class PerfRecorder implements PerfSink {
  private readonly active = new Map<string, Trace>();
  private readonly completed: Trace[] = [];
  private readonly aliases = new Map<string, string>();
  readonly counters: PerfCounters = { duplicateIds: 0, droppedMarks: 0, evictedActive: 0 };
  /** Optional hook run after each trace finishes (used to sample resources; never given content). */
  onTraceEnd: (() => void) | null = null;

  constructor(private readonly now: () => number = () => performance.now()) {}

  static newId(): string {
    return randomUUID();
  }

  /** Start a trace. A duplicate id (active or completed) is refused and counted, never merged. */
  begin(kind: PerfKind, id: string, parent?: string): boolean {
    if (!KIND_SET.has(kind) || !UUID.test(id)) {
      this.counters.droppedMarks += 1;
      return false;
    }
    if (this.active.has(id) || this.completed.some((trace) => trace.id === id)) {
      this.counters.duplicateIds += 1;
      return false;
    }
    if (this.active.size >= MAX_ACTIVE_TRACES) {
      const oldest = this.active.keys().next().value as string;
      this.active.delete(oldest);
      this.counters.evictedActive += 1;
    }
    const trace: Trace = {
      id,
      kind,
      parent: parent !== undefined && UUID.test(parent) ? parent : null,
      marks: new Map(),
      values: new Map(),
      outcome: null,
    };
    this.active.set(id, trace);
    return true;
  }

  /** Record a stage. The first mark of a stage wins; unknown ids or stage names are dropped. */
  mark(id: string, stage: PerfStage): void {
    const trace = this.active.get(id);
    if (!trace || !STAGE_SET.has(stage)) {
      this.counters.droppedMarks += 1;
      return;
    }
    if (!trace.marks.has(stage)) trace.marks.set(stage, this.now());
  }

  /** A number that is not a duration (a server-reported time, a size). Content cannot fit here. */
  value(id: string, name: PerfValue, amount: number): void {
    const trace = this.active.get(id);
    if (
      !trace ||
      !VALUE_SET.has(name) ||
      typeof amount !== "number" ||
      !Number.isFinite(amount) ||
      amount < 0
    ) {
      this.counters.droppedMarks += 1;
      return;
    }
    trace.values.set(name, amount);
  }

  /** Let a later trace refer to this one by another id (e.g. an extraction id for a capture id). */
  alias(alias: string, id: string): void {
    if (!UUID.test(alias) || !UUID.test(id)) return;
    if (this.aliases.size >= MAX_ALIASES) {
      const oldest = this.aliases.keys().next().value as string;
      this.aliases.delete(oldest);
    }
    this.aliases.set(alias, id);
  }

  /** Finish a trace (ok, failed or cancelled). Ending an unknown or finished id is a no-op. */
  end(id: string, outcome: PerfOutcome): void {
    const trace = this.active.get(id);
    if (!trace) return;
    this.active.delete(id);
    trace.outcome = outcome;
    this.completed.push(trace);
    if (this.completed.length > MAX_COMPLETED_TRACES) this.completed.shift();
    this.onTraceEnd?.();
  }

  /** Elapsed ms between two stages of a trace, or null if either is missing. */
  duration(id: string, from: PerfStage, to: PerfStage): number | null {
    const trace = this.find(id);
    if (!trace) return null;
    const a = trace.marks.get(from);
    const b = trace.marks.get(to);
    if (a === undefined || b === undefined || b < a) return null;
    return b - a;
  }

  activeCount(): number {
    return this.active.size;
  }

  completedCount(): number {
    return this.completed.length;
  }

  private find(id: string): Trace | undefined {
    const resolved = this.aliases.get(id) ?? id;
    return this.active.get(resolved) ?? this.completed.find((trace) => trace.id === resolved);
  }

  /** Raw successful samples per metric (ms). Failed and cancelled traces are only counted. */
  samples(): Record<string, number[]> {
    const out: Record<string, number[]> = {};
    const add = (name: string, value: number | null): void => {
      if (value === null) return;
      (out[name] ??= []).push(Math.round(value * 100) / 100);
    };
    for (const trace of this.completed) {
      if (trace.outcome !== "ok") continue;
      for (const spec of METRICS) {
        if (spec.kind !== trace.kind) continue;
        add(spec.name, this.duration(trace.id, spec.from, spec.to));
      }
      const server = trace.values.get("server_ms");
      const request =
        trace.kind === "audio"
          ? this.duration(trace.id, "request_start", "response")
          : trace.kind === "screenshot"
            ? this.duration(trace.id, "upload_start", "extracted")
            : null;
      if (server !== undefined) add(`${trace.kind}.server_reported_ms`, server);
      // Derived, not measured: the part of the round trip the server did not account for. It is the
      // network plus client-side overhead (request build, TLS, body read), never provider time.
      if (server !== undefined && request !== null) {
        add(`${trace.kind}.network_and_client_overhead_ms`, Math.max(0, request - server));
      }
      const retrieval = trace.values.get("retrieval_ms");
      if (retrieval !== undefined) add("answer.server_retrieval_ms", retrieval);
      if (trace.kind === "answer" && trace.parent) {
        const total = this.chain(trace);
        if (total !== null) add("answer.input_to_done_ms", total);
      }
    }
    return out;
  }

  /** From the first mark of the parent trace (a capture or an utterance) to this answer's `done`. */
  private chain(trace: Trace): number | null {
    const parent = this.find(trace.parent ?? "");
    const done = trace.marks.get("done");
    if (!parent || done === undefined) return null;
    const starts = [...parent.marks.values()];
    if (starts.length === 0) return null;
    const first = Math.min(...starts);
    return done >= first ? done - first : null;
  }

  summary(): {
    metrics: Record<string, Stats>;
    outcomes: Record<string, Record<PerfOutcome, number>>;
    counters: PerfCounters;
    scopes: Record<string, string>;
  } {
    const metrics: Record<string, Stats> = {};
    for (const [name, values] of Object.entries(this.samples())) {
      const computed = stats(values);
      if (computed) metrics[name] = computed;
    }
    const outcomes: Record<string, Record<PerfOutcome, number>> = {};
    for (const trace of this.completed) {
      const bucket = (outcomes[trace.kind] ??= { ok: 0, error: 0, cancelled: 0 });
      if (trace.outcome) bucket[trace.outcome] += 1;
    }
    const scopes: Record<string, string> = {};
    for (const spec of METRICS) scopes[spec.name] = spec.scope;
    return { metrics, outcomes, counters: { ...this.counters }, scopes };
  }
}

// ----------------------------------------------------------------------------- resources

export type ProcessMetric = {
  type: string;
  cpu: { percentCPUUsage: number };
  memory: { workingSetSize: number }; // KB, as Electron reports it
};

export type ResourceSummary = Record<
  string,
  { samples: number; memory_mb: Stats; cpu_percent: Stats }
>;

export const MAX_RESOURCE_SAMPLES = 2_000;

/**
 * Samples `app.getAppMetrics()` through an injected function; keeps numbers only. There is no timer:
 * the main process has a standing rule against periodic timers, so it is sampled at events (start,
 * each finished operation, quit) through `PerfRecorder.onTraceEnd`.
 */
export class ResourceSampler {
  private readonly byType = new Map<string, { mem: number[]; cpu: number[] }>();

  constructor(private readonly read: () => readonly ProcessMetric[]) {}

  sample(): void {
    for (const metric of this.read()) {
      if (typeof metric.type !== "string" || metric.type.length > 32) continue;
      const mem = metric.memory?.workingSetSize;
      const cpu = metric.cpu?.percentCPUUsage;
      if (!Number.isFinite(mem) || !Number.isFinite(cpu)) continue;
      const entry = this.byType.get(metric.type) ?? { mem: [], cpu: [] };
      entry.mem.push(mem / 1024);
      entry.cpu.push(cpu);
      if (entry.mem.length > MAX_RESOURCE_SAMPLES) {
        entry.mem.shift();
        entry.cpu.shift();
      }
      this.byType.set(metric.type, entry);
    }
  }

  summary(): ResourceSummary {
    const out: ResourceSummary = {};
    for (const [type, entry] of this.byType) {
      const mem = stats(entry.mem);
      const cpu = stats(entry.cpu);
      if (mem && cpu) out[type] = { samples: entry.mem.length, memory_mb: mem, cpu_percent: cpu };
    }
    return out;
  }
}

// ----------------------------------------------------------------------------- report

export type PerfEnvironment = {
  appVersion: string;
  electron: string;
  node: string;
  platform: string;
  arch: string;
  osRelease: string;
  cpuModel: string;
  cpuCount: number;
  totalMemoryMb: number;
  /** Seconds the process had been alive when the first mark was taken (Electron boot, not ours). */
  processUptimeAtModuleLoadS: number;
};

/** The report file body. Timing numbers and machine specs only; it has no field for content. */
export function buildReport(
  recorder: PerfRecorder,
  sampler: ResourceSampler,
  environment: PerfEnvironment,
  generatedAt: string,
): string {
  return JSON.stringify(
    {
      generated_at: generatedAt,
      environment,
      clock: "performance.now (monotonic, ms)",
      summary: recorder.summary(),
      samples: recorder.samples(),
      resources: sampler.summary(),
    },
    null,
    2,
  );
}

/** The one shared recorder for the main process. */
export const perf = new PerfRecorder();
