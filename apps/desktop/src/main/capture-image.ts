/** Pure image helpers for screenshot capture (no Electron, no I/O). */

export const MAX_LONG_EDGE = 2560;
export const PREVIEW_LONG_EDGE = 640;

/** Scale (width, height) down so the long edge is at most `maxLongEdge`; never scales up. */
export function scaledSize(
  width: number,
  height: number,
  maxLongEdge: number,
): { width: number; height: number } {
  const w = Math.max(1, Math.round(width));
  const h = Math.max(1, Math.round(height));
  const long = Math.max(w, h);
  if (long <= maxLongEdge) return { width: w, height: h };
  const ratio = maxLongEdge / long;
  return {
    width: Math.max(1, Math.round(w * ratio)),
    height: Math.max(1, Math.round(h * ratio)),
  };
}

/**
 * True when a BGRA/RGBA bitmap is entirely black or fully transparent, which is what protected or
 * GPU-composited content looks like to the capture API. Samples evenly, so it is cheap.
 */
export function isBlankBitmap(bitmap: Uint8Array): boolean {
  if (bitmap.length < 4) return true;
  const pixels = Math.floor(bitmap.length / 4);
  const step = Math.max(1, Math.floor(pixels / 4096));
  for (let pixel = 0; pixel < pixels; pixel += step) {
    const offset = pixel * 4;
    if (bitmap[offset] !== 0 || bitmap[offset + 1] !== 0 || bitmap[offset + 2] !== 0) return false;
  }
  return true;
}
