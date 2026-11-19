/**
 * Window placement types shared by the main process and the renderer (M-6, D-71). The renderer only
 * ever sees a short description of each display and whether the assistant is on it. It cannot set
 * coordinates: the only action it can request is "move to this display", validated in the main
 * process.
 */
export type WindowMode = "compact" | "expanded";

export type DisplayChoice = {
  /** The operating system's display id (stable for a monitor in practice, but never assumed). */
  id: number;
  /** For example "Display 2 · 2560×1440 · 150% · at 1920, 0". */
  label: string;
  primary: boolean;
  /** True when most of the assistant window is on this display. */
  current: boolean;
  width: number;
  height: number;
  scalePercent: number;
};

export type DisplayList = { displays: DisplayChoice[] };
