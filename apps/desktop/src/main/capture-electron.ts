/**
 * The ONLY module that touches the screen (Phase 11B, D-51): Electron `desktopCapturer`, `screen`
 * and `nativeImage`. It is reachable only through `CaptureService`, which requires an explicit
 * start, a visible assistant window and a completed countdown. Never imported by the preload or
 * the renderer, and it never writes to disk: images exist only as in-memory buffers.
 *
 * Limitation (documented): `desktopCapturer.getSources` renders every screen to build its list, so
 * frames of the displays that were not chosen are produced and dropped immediately; they are never
 * kept, previewed or sent. The display picker itself requests no thumbnails at all.
 */
import { desktopCapturer, nativeImage, screen } from "electron";
import type { CaptureDisplay } from "../shared/contracts";
import { isBlankBitmap, MAX_LONG_EDGE, PREVIEW_LONG_EDGE, scaledSize } from "./capture-image";
import { MAX_PNG_BYTES } from "./capture-service";

export function listElectronDisplays(): CaptureDisplay[] {
  const primaryId = screen.getPrimaryDisplay().id;
  return screen.getAllDisplays().map((display, index) => ({
    id: String(display.id),
    label: `Display ${index + 1}${display.id === primaryId ? " (primary)" : ""} · ${display.size.width}×${display.size.height}`,
    primary: display.id === primaryId,
    width: display.size.width,
    height: display.size.height,
  }));
}

export async function captureElectronDisplay(displayId: string): Promise<Buffer | null> {
  const displays = screen.getAllDisplays();
  const index = displays.findIndex((display) => String(display.id) === displayId);
  if (index < 0) return null;
  const display = displays[index];
  if (!display) return null;
  const target = scaledSize(
    display.size.width * display.scaleFactor,
    display.size.height * display.scaleFactor,
    MAX_LONG_EDGE,
  );
  const sources = await desktopCapturer.getSources({ types: ["screen"], thumbnailSize: target });
  const source =
    sources.find((item) => item.display_id === displayId) ??
    (sources.length === displays.length ? sources[index] : undefined);
  if (!source) return null;
  let image = source.thumbnail;
  if (image.isEmpty() || isBlankBitmap(image.toBitmap())) return null;
  let png = image.toPNG();
  // Keep the text legible but under the size cap: shrink in steps, never change the format.
  for (let attempt = 0; attempt < 4 && png.length > MAX_PNG_BYTES; attempt += 1) {
    const size = image.getSize();
    image = image.resize({ width: Math.max(64, Math.round(size.width * 0.75)) });
    png = image.toPNG();
  }
  return png.length <= MAX_PNG_BYTES ? png : null;
}

export function makeElectronPreview(png: Buffer): {
  dataUrl: string;
  width: number;
  height: number;
} {
  const image = nativeImage.createFromBuffer(png);
  const size = image.getSize();
  const preview = scaledSize(size.width, size.height, PREVIEW_LONG_EDGE);
  const small = size.width > preview.width ? image.resize({ width: preview.width }) : image;
  return { dataUrl: small.toDataURL(), width: size.width, height: size.height };
}
