/**
 * Update state shared by the main process and the renderer (M-11, D-78).
 *
 * The renderer only ever sees this plain, content-free state. It cannot start a download or an
 * install except through the named IPC channels, which the main process also guards by state.
 */

export type UpdateDisabledReason =
  /** Running from source (`npm run dev`). */
  | "development"
  /** No update feed was built into this installer. */
  | "not_configured"
  /** The feed is not an https URL. */
  | "insecure_feed"
  /** An unsigned build: its publisher cannot be verified, so it never self-updates. */
  | "unsigned_build"
  /** Turned off with INTERVIEW_DISABLE_UPDATE_CHECK=1. */
  | "disabled_by_environment";

export type UpdateErrorCode =
  | "network"
  | "feed_unavailable"
  | "manifest_invalid"
  | "version_mismatch"
  | "download_failed"
  | "insufficient_disk_space"
  | "verification_failed"
  | "install_failed"
  | "timeout"
  | "unknown";

export type UpdateState =
  | { status: "disabled"; reason: UpdateDisabledReason }
  | { status: "idle"; lastCheckedAt: string | null }
  | { status: "checking" }
  | { status: "none"; checkedAt: string }
  | { status: "available"; version: string; sizeBytes: number }
  | { status: "downloading"; version: string; percent: number }
  | { status: "downloaded"; version: string }
  | { status: "installing"; version: string }
  | { status: "error"; code: UpdateErrorCode; message: string; retryable: boolean };

export type UpdateStatus = UpdateState["status"];

/** Fixed, non-sensitive text for each error code (never a raw error message or URL). */
export const UPDATE_ERROR_MESSAGES: Record<UpdateErrorCode, string> = {
  network: "Could not reach the update server. Check your connection and try again.",
  feed_unavailable: "The update server is not available right now. Try again later.",
  manifest_invalid: "The update information is not valid, so it was ignored.",
  version_mismatch: "The update does not match what was announced, so it was ignored.",
  download_failed: "The update could not be downloaded. You can try again.",
  insufficient_disk_space: "There is not enough free disk space to download the update.",
  verification_failed: "The downloaded update could not be verified, so it was discarded.",
  install_failed: "The update could not be installed. The current version is unchanged; you can try again.",
  timeout: "The update server took too long to respond. Try again later.",
  unknown: "The update could not be completed. Try again later.",
};
