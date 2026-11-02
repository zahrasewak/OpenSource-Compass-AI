// Generates simple PNG icons (a compass-like disc) without any image dependency.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { crc32, deflateSync } from "node:zlib";

function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type), data]);
  const out = Buffer.alloc(8 + data.length + 4);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body) >>> 0, 8 + data.length);
  return out;
}

function png(size) {
  const px = Buffer.alloc(size * (size * 4 + 1));
  const c = (size - 1) / 2;
  for (let y = 0; y < size; y++) {
    const row = y * (size * 4 + 1);
    px[row] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      const d = Math.hypot(x - c, y - c) / (size / 2);
      const needle = Math.abs(x - c) + Math.abs(y - c) < size * 0.28 && Math.abs(x - y) < size * 0.1; // diagonal needle
      let rgba = [0, 0, 0, 0];
      if (d <= 1) rgba = needle ? [255, 255, 255, 255] : d > 0.82 ? [88, 52, 160, 255] : [110, 64, 201, 255];
      px.set(rgba, row + 1 + x * 4);
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(px)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

export function makeIcons(dir) {
  mkdirSync(dir, { recursive: true });
  for (const s of [16, 32, 48, 128]) writeFileSync(resolve(dir, `icon${s}.png`), png(s));
}
