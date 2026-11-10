// Generates build/icon.png (256x256) and build/icon.ico from the project-owned
// tray artwork (rounded square + three bars). No dependencies; deterministic.
import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";

const N = 256, SS = 4, S = N * SS, k = S / 32; // artwork is authored on a 32-unit grid
const bg = [0x67, 0xe8, 0xf9], fg = [0x08, 0x2f, 0x49];
const rects = [[9, 10, 14, 3], [9, 16, 14, 3], [9, 22, 9, 3]];
function inRound(x, y) { // rect 2..30, r=9
  const x0 = 2 * k, x1 = 30 * k, r = 9 * k;
  if (x < x0 || x > x1 || y < x0 || y > x1) return false;
  const cx = x < x0 + r ? x0 + r : x > x1 - r ? x1 - r : x;
  const cy = y < x0 + r ? x0 + r : y > x1 - r ? x1 - r : y;
  return (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
}
const inBar = (x, y) => rects.some(([a, b, w, h]) => x >= a * k && x < (a + w) * k && y >= b * k && y < (b + h) * k);
const raw = Buffer.alloc(N * (N * 4 + 1));
for (let y = 0; y < N; y++) {
  raw[y * (N * 4 + 1)] = 0;
  for (let x = 0; x < N; x++) {
    let a = 0, c = [0, 0, 0];
    for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
      const px = x * SS + sx + 0.5, py = y * SS + sy + 0.5;
      if (!inRound(px, py)) continue;
      const col = inBar(px, py) ? fg : bg;
      a++; c = c.map((v, i) => v + col[i]);
    }
    const o = y * (N * 4 + 1) + 1 + x * 4;
    if (a) { raw[o] = Math.round(c[0] / a); raw[o + 1] = Math.round(c[1] / a); raw[o + 2] = Math.round(c[2] / a); }
    raw[o + 3] = Math.round((255 * a) / (SS * SS));
  }
}
const crcTable = Array.from({ length: 256 }, (_, n) => { let c = n; for (let i = 0; i < 8; i++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
const crc = (b) => { let c = 0xffffffff; for (const v of b) c = crcTable[(c ^ v) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
const chunk = (t, d) => { const l = Buffer.alloc(4); l.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([l, td, c]); };
const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(N, 0); ihdr.writeUInt32BE(N, 4); ihdr[8] = 8; ihdr[9] = 6;
const png = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw, { level: 9 })), chunk("IEND", Buffer.alloc(0))]);
const dir = new URL("../build/", import.meta.url);
mkdirSync(dir, { recursive: true });
writeFileSync(new URL("icon.png", dir), png);
// ICO with a single PNG-compressed 256x256 image (width/height byte 0 = 256).
const head = Buffer.alloc(22);
head.writeUInt16LE(0, 0); head.writeUInt16LE(1, 2); head.writeUInt16LE(1, 4);
head[6] = 0; head[7] = 0; head.writeUInt16LE(1, 10); head.writeUInt16LE(32, 12);
head.writeUInt32LE(png.length, 14); head.writeUInt32LE(22, 18);
writeFileSync(new URL("icon.ico", dir), Buffer.concat([head, png]));
console.log("icon.png", png.length, "bytes");
