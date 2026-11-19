/**
 * Crash recovery and diagnostics: types shared by the main process and the renderer (M-5, D-70).
 * Nothing here can carry content: the renderer only learns that a recovery happened, and a count of
 * characters copied. The diagnostics text itself never crosses into the renderer.
 */
export type RecoveryInfo = {
  /**
   * "recovered" once, after the window was reloaded because its renderer stopped. It is cleared as
   * soon as the renderer has read it. It never means anything was restarted.
   */
  notice: "recovered" | null;
};

export type CopyDiagnosticsResult = {
  characters: number;
  /** True when the report was cut to stay within the size limit. */
  truncated: boolean;
};

export const RECOVERY_NOTICE_TEXT =
  "The window stopped working and was reloaded. You are still signed in. Listening and screenshots were stopped, and nothing was restarted for you.";
