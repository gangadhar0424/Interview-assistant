/**
 * Acceptable-use acknowledgement (M-1, D-68). The wording and its version live here so the renderer
 * (which shows it) and the main process (which enforces it and shows the native confirmation) can
 * never disagree. Raising `ACCEPTABLE_USE_VERSION` makes every user acknowledge the new text again.
 *
 * The wording is a policy statement for the owner to approve, not legal advice (OQ-15).
 */
export const ACCEPTABLE_USE_VERSION = 1;

export const ACCEPTABLE_USE_TITLE = "Acceptable use";

export const ACCEPTABLE_USE_INTRO =
  "Before you use audio or screenshot assistance, please confirm the following.";

export const ACCEPTABLE_USE_STATEMENTS: readonly string[] = [
  "I am responsible for following the rules of any interview, meeting or assessment where I use this app. If those rules do not allow AI assistance, recording or screenshots, I will not use it there.",
  "I will capture or process audio and screenshots only when I have permission to do so, including the permission of other people whose voice or screen may be captured, and as the law requires.",
  "I will not use this app in any way that the relevant interview or assessment rules prohibit.",
];

export const ACCEPTABLE_USE_NOTE =
  "This app always shows when it is listening or capturing. It does not hide itself and must not be used to avoid monitoring.";

/** Error code the main process returns when capture is attempted before acknowledgement. */
export const ACKNOWLEDGEMENT_REQUIRED_CODE = "acknowledgement_required";
export const ACKNOWLEDGEMENT_REQUIRED_MESSAGE =
  "Read and accept the acceptable-use notice before using audio or screenshots.";

export type AcknowledgementState = {
  /** True only for the current wording version, accepted by the signed-in user on this device. */
  accepted: boolean;
  /** The wording version the app currently requires. */
  version: number;
  acceptedAt: string | null;
};
