/**
 * Media permission rules (Phase 12, D-53): everything stays denied EXCEPT audio capture while the
 * user has explicitly pressed Start and the main process is waiting for that capture.
 *
 * Pure functions: no Electron import, so the rules are unit-tested exhaustively. `index.ts` wires
 * them into `setPermissionRequestHandler`, `setPermissionCheckHandler` and
 * `setDisplayMediaRequestHandler`. Video, display capture (screen pixels) and every other
 * permission are always denied.
 */

export type AudioSource = "microphone" | "system";
export type AudioGateState = {
  state: "idle" | "arming" | "listening";
  source: AudioSource | null;
};

export const IDLE_GATE: AudioGateState = { state: "idle", source: null };

export type PermissionQuery = {
  kind: "request" | "check";
  permission: string;
  /** For requests: the requested media types. For checks: the single media type, if known. */
  mediaTypes: readonly string[];
  requestingUrl: string;
  gate: AudioGateState;
  isTrustedRenderer: (url: string) => boolean;
};

/** Microphone access: audio only, trusted renderer only, and only for an armed microphone capture. */
export function decideMediaPermission(query: PermissionQuery): boolean {
  if (query.permission !== "media") return false;
  if (!query.isTrustedRenderer(query.requestingUrl)) return false;
  if (query.gate.source !== "microphone") return false;
  const audioOnly = query.mediaTypes.length > 0 && query.mediaTypes.every((type) => type === "audio");
  if (!audioOnly) return false;
  // A request is allowed only while arming (between Start and the first audio); a check (device
  // enumeration, an already granted stream) also while listening.
  return query.kind === "request"
    ? query.gate.state === "arming"
    : query.gate.state === "arming" || query.gate.state === "listening";
}

export type DisplayMediaStreams = { audio: "loopback" } | null;

/**
 * `getDisplayMedia` is only ever answered with system AUDIO (never a video source, so no screen
 * pixels exist), only for a trusted renderer and only while a system-audio capture is arming.
 */
export function decideDisplayMedia(
  gate: AudioGateState,
  requestingUrl: string,
  isTrustedRenderer: (url: string) => boolean,
): DisplayMediaStreams {
  if (gate.state !== "arming" || gate.source !== "system") return null;
  if (!isTrustedRenderer(requestingUrl)) return null;
  return { audio: "loopback" };
}
