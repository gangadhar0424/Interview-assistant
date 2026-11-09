export type WindowPort = {
  getBounds(): { x: number; y: number; width: number; height: number };
  setSize(width: number, height: number, animate?: boolean): void;
  setMinimumSize(width: number, height: number): void;
  setOpacity(opacity: number): void;
  setAlwaysOnTop(flag: boolean): void;
};

export type CloseEventPort = { preventDefault(): void };

export const WINDOW_SIZE = {
  compact: { width: 390, height: 520 },
  expanded: { width: 760, height: 740 },
} as const;

export function setExpanded(window: WindowPort, expanded: boolean): void {
  const { width, height } = expanded ? WINDOW_SIZE.expanded : WINDOW_SIZE.compact;
  window.setMinimumSize(360, 460);
  window.setSize(width, height, true);
}

export function applyWindowSettings(
  window: WindowPort,
  settings: { opacity: number; alwaysOnTop: boolean },
): void {
  window.setOpacity(Math.max(0.65, Math.min(1, settings.opacity)));
  window.setAlwaysOnTop(settings.alwaysOnTop);
}

export function closeToTray(
  event: CloseEventPort,
  window: { hide(): void },
  quitting: boolean,
): void {
  if (quitting) return;
  event.preventDefault();
  window.hide();
}
