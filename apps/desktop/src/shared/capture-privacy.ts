/**
 * Window capture privacy (M-15, merges M-14). Shared between the main process (which applies and
 * enforces it) and the renderer (which explains it and shows the indicator).
 *
 * What the feature does: when the user turns it on, the app asks Windows (through Electron's
 * `BrowserWindow.setContentProtection`, which sets the window display affinity) to leave THIS app's
 * window out of supported screen-capture operations. It is a privacy and accessibility setting for
 * keeping private material (a resume, notes) out of a screen share the user is allowed to make.
 *
 * What it does NOT do, and must never claim: hide the app from everything, hide the tray icon, the
 * taskbar entry or the process, or defeat monitoring, proctoring or security software.
 */

/** Windows 10 version 2004 (build 19041) is the first with WDA_EXCLUDEFROMCAPTURE. */
export const MIN_SUPPORTED_WINDOWS_BUILD = 19041;

/**
 * Version of the consent wording specific to THIS setting (separate from the acceptable-use notice).
 * Raising it makes every saved "on" stop applying until the user consents again to the new text.
 */
export const CAPTURE_PRIVACY_CONSENT_VERSION = 1;

/** The user's recorded consent to this setting: which wording, and when. Local to the device. */
export type CapturePrivacyConsent = { version: number; acceptedAt: string };

export type CapturePrivacyStatus =
  /** The setting is off (the default). Nothing was requested from Windows. */
  | "off"
  /** The setting is on and Windows was asked to exclude the window. Electron does not report whether it took effect. */
  | "requested"
  /** This platform or Windows build cannot do it. The setting stays off and the app works normally. */
  | "unsupported"
  /** The request threw. The window is shown normally, as before, and the app works normally. */
  | "failed";

export type CapturePrivacyState = {
  /** What the user chose (persisted). Only ever true after an explicit, confirmed action. */
  enabled: boolean;
  status: CapturePrivacyStatus;
  /** User-safe, plain-language status. Never claims the window is invisible to everything. */
  message: string;
  /** Whether this device can do it at all (platform and Windows build). */
  supported: boolean;
  /** The recorded consent for the current wording, or null when none (always null while off). */
  consent: CapturePrivacyConsent | null;
};

export const CAPTURE_PRIVACY_TITLE = "Hide this window from screen capture";

export const CAPTURE_PRIVACY_EXPLANATION: readonly string[] = [
  "When this is on, the app asks Windows to leave the Interview Assistant window out of screen capture that supports the request, such as many screen-sharing and recording apps. It is meant for keeping private material, like your resume or notes, from showing in a screen share you are allowed to make, and for accessibility.",
  "It only affects this app's own window. The app still appears in the taskbar and the system tray, still shows its listening and capture indicators, and is still listed as a running program. In a Windows remote session the window is not excluded, so remote-access and remote-support tools still see it.",
  "It is not a guarantee. Some capture methods, other screens, cameras pointed at your display, and security, proctoring or monitoring software may still see the window. It does not and must not be used to avoid monitoring.",
  "You remain responsible for following the rules of any interview, meeting or assessment where you use this app. If those rules do not allow AI assistance, do not use it there.",
];

export const CAPTURE_PRIVACY_INDICATOR =
  "Hidden from screen capture: this window has been requested to be left out of supported captures. Not guaranteed.";

export const CAPTURE_PRIVACY_MESSAGES: Record<CapturePrivacyStatus, string> = {
  off: "Off. This window can be captured like any other window.",
  requested:
    "On. Windows was asked to leave this window out of supported screen capture. This is not a guarantee, and the app does not check whether a given tool honours it.",
  unsupported:
    "Not available on this device. It needs Windows 10 version 2004 (build 19041) or later. The app works normally and the window is shown as usual.",
  failed:
    "Windows did not accept the request. The window is shown as usual and the app works normally. You can try again.",
};

/** Error codes the main process returns for this feature. */
export const CAPTURE_PRIVACY_ACK_REQUIRED_CODE = "acknowledgement_required";
export const CAPTURE_PRIVACY_ACK_REQUIRED_MESSAGE =
  "Read and accept the acceptable-use notice before turning on screen-capture privacy.";

/** The Windows build number from `os.release()` ("10.0.26300" gives 26300), or null if unrecognised. */
export function parseWindowsBuild(release: string): number | null {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(release);
  if (!match) return null;
  const major = Number(match[1]);
  const build = Number(match[3]);
  if (major < 10 || !Number.isInteger(build) || build < 0) return null;
  return build;
}

export function isCapturePrivacySupported(platform: string, release: string): boolean {
  if (platform !== "win32") return false;
  const build = parseWindowsBuild(release);
  return build !== null && build >= MIN_SUPPORTED_WINDOWS_BUILD;
}
